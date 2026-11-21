"""The dev teardown-and-reprovision proof: archive, destroy dev_ only, bootstrap from empty, verify.

`plan` reads and writes nothing in the workspace or this machine's state; `run --yes` holds
bootstrap's dev lock throughout and hands it to the bootstrap it starts; `finish` bootstraps a
run's dev again under the same lock; `verify` and `verify-agent` only read. Each step writes its report under `--out`, which a run never reuses.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import signal
import subprocess
import sys
from collections.abc import Callable, Iterator
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any, NoReturn

from weather_signal.agent import launchd
from weather_signal.deploy import destroy, genie, lock, preflight, proof_verify, schedules, scope
from weather_signal.deploy import proof_archive as pa
from weather_signal.deploy import proof_checks as pc
from weather_signal.deploy import proof_record as record
from weather_signal.deploy.cli import Stop, Unknown, not_found
from weather_signal.deploy.context import Ctx
from weather_signal.deploy.lock import Busy

PROFILE_AGENT = launchd.PROFILES[0]
# dev's prevent_destroy holds (databricks.yml) lifted for this destroy only; prod's are literals.
HOLDS_OFF = ("--var", "dev_holds=false")
RESIDUE_WAIT_S, RESIDUE_EVERY_S = 900.0, 15.0  # an App's delete finishes minutes after destroy
SCORE_EVERY_S = 60.0
Stream = Callable[[list[str], Path, int], int]  # argv, log, the lock descriptor it inherits


@dataclass
class Proof:
    ctx: Ctx  # bootstrap's own context on the dev scope
    out: Path
    seed_from: Path
    main: Path  # the checkout the proof runs from
    python: str
    read: Callable[[str], bytes | None]
    stream: Stream
    paused: bool = False  # set once the run starts pausing dev
    destroying: bool = False  # set once the destroy may have begun, or on a resume
    bootstrapping: bool = False  # set once the bootstrap starts: dev is being made again
    get: Callable[[str], tuple[int, bytes]] | None = None  # the App over HTTP, workspace auth

    def write(self, name: str, text: str) -> Path:
        path = self.out / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def say(self, line: str) -> None:
        self.ctx.say(f"proof: {line}")


def bootstrap_argv(p: Proof) -> list[str]:
    exe = str(Path(p.python).parent / "weather-signal")
    argv = [exe, "bootstrap", "--profile", p.ctx.scope.profile, "--target", "dev"]
    return [*argv, "--seed-from", str(p.seed_from), "--ledger-from", str(_ledger(p))]


def _ledger(p: Proof) -> Path:
    return p.out / "archive" / "ledger"


def refusals(p: Proof) -> list[str]:
    """What stops the proof before it reads the workspace."""
    pc.refuse_unless_dev(p.ctx.scope)
    out = []
    linked = p.ctx.linked_main(p.main)
    if linked is not None:
        out.append(f"{p.main} is a linked worktree; run the proof from the main checkout {linked}")
    if Path(p.python).parent != p.main / ".venv-connect" / "bin":
        out.append(f"run under {p.main}/.venv-connect (the LaunchAgent keeps bootstrap's Python)")
    if not (p.seed_from / "manifest.json").is_file():
        out.append(f"{p.seed_from} has no manifest.json; pass the data folder as --seed-from")
    return out


def deployed_state(p: Proof) -> dict[str, Any] | None:
    """The dev bundle's deployed state: exactly what `bundle destroy` deletes. None when absent."""
    me = str((p.ctx.cli("current-user", "me") or {}).get("userName") or "")
    if not me:
        raise Unknown("current-user me gave no userName")
    path = f"/Workspace/Users/{me}/.bundle/weather-signal/dev/state/resources.json"
    r = p.ctx.run(["databricks", "workspace", "export", path, "--profile", p.ctx.scope.profile])
    if not_found(r):
        return None
    if r.rc != 0:
        raise Unknown(f"workspace export {path}: exit {r.rc}: {r.tail(3)}")
    try:
        doc: dict[str, Any] = json.loads(r.out)
    except json.JSONDecodeError as err:
        raise Unknown(f"{path} is not JSON") from err
    state = doc.get("state") if isinstance(doc, dict) else None
    return None if isinstance(state, dict) and not resource_keys(doc) else doc


def resource_keys(state: dict[str, Any]) -> list[str]:
    """The deployed resources `bundle destroy` deletes: `resources.<type>.<name>` keys."""
    inner = state.get("state")
    return sorted(k for k in inner if str(k).count(".") == 2) if isinstance(inner, dict) else []


def _guard_state(p: Proof) -> dict[str, Any] | None:
    state = deployed_state(p)
    bad = pc.destroy_refusals(state, p.ctx.scope) if state is not None else []
    if bad:
        raise Stop(
            2, "the dev state holds non-dev_ resources; nothing destroyed:\n  " + "\n  ".join(bad)
        )
    return state


def teardown(
    p: Proof, destroy_state: bool, last_check: Callable[[], list[str]] | None = None
) -> list[str]:
    """`bundle destroy` of the dev state the guard passed (only with `destroy_state`), its Genie
    space, then no dev_ residue. `last_check` runs after every other read, right before the destroy."""
    ctx, lines = p.ctx, []
    state = _guard_state(p) if destroy_state else None
    if state is not None:
        ctx.warehouse_id = preflight._warehouse(ctx)
        lines += last_check() if last_check else []
        record.destroy_began(p, resource_keys(state))
        p.destroying = True
        r = ctx.run([*ctx.bundle("destroy"), *HOLDS_OFF, "--auto-approve"])
        p.write("destroy-bundle.log", r.err + r.out)
        if r.rc != 0:
            msg = f"bundle destroy failed (exit {r.rc}):\n{r.tail()}"
            _destroy_failed(p, resource_keys(state), last_check, msg)
        lines.append("bundle destroy: exit 0")
    lines += [f"genie: trashed {space}" for space in genie.trash(ctx)]
    schedules.release(ctx)
    waited = 0.0
    while left := destroy.residue(ctx):
        if waited >= RESIDUE_WAIT_S:
            raise Stop(1, f"dev residue after {RESIDUE_WAIT_S:.0f} s: {', '.join(left)}")
        ctx.sleep(RESIDUE_EVERY_S)
        waited += RESIDUE_EVERY_S
    return [*lines, "residue: none"]


def _destroy_failed(
    p: Proof, keys: list[str], recount: Callable[[], list[str]] | None, msg: str
) -> NoReturn:
    """A failed destroy that left every resource deployed and the archive's data as archived
    closes the run: no --resume, no pointer. Anything less, or unknown, keeps it resumable."""
    untouched = False
    if recount is not None:  # a resume holds no archive in memory to recount against
        try:
            untouched = resource_keys(deployed_state(p) or {}) == keys and bool(recount())
        except (Stop, Unknown):
            untouched = False
    if not untouched:
        raise Stop(1, msg)
    record.destroy_failed(p)
    p.destroying = False
    raise Stop(1, f"{msg}\nnothing was deleted: all {len(keys)} resources are deployed and the "
                  "recount matches the archive. This run is closed, not resumable. To unpause dev "
                  f"and reload the agent: {way_back(p)}; then run the proof with a new --out")  # fmt: skip


def _set_aside(p: Proof) -> None:
    """A resumed run keeps the earlier bootstrap's record as `bootstrap-<n>.*`."""
    if not (p.out / "bootstrap.json").exists():
        return
    n = 1
    while (p.out / f"bootstrap-{n}.json").exists():
        n += 1
    # The log goes first: a stop between the two renames leaves bootstrap.json to finish the pair.
    for ext in ("log", "json"):
        if (p.out / f"bootstrap.{ext}").exists():
            (p.out / f"bootstrap.{ext}").rename(p.out / f"bootstrap-{n}.{ext}")


def bootstrap(p: Proof, fd: int) -> int:
    _set_aside(p)
    argv = bootstrap_argv(p)
    doc: dict[str, Any] = {"argv": argv, "rc": None, "began": p.ctx.now().isoformat()}
    p.write("bootstrap.json", json.dumps({**doc, "ended": None}, indent=1) + "\n")
    rc = p.stream(argv, p.out / "bootstrap.log", fd)
    record.bootstrap_ended(p, rc)
    doc |= {"rc": rc, "ended": p.ctx.now().isoformat()}
    p.write("bootstrap.json", json.dumps(doc, indent=1) + "\n")
    return rc


def plan(p: Proof) -> int:
    """Everything `run` would do, from read-only probes; writes `plan.md` and nothing else."""
    bad = refusals(p)
    state = deployed_state(p)
    bad += pc.destroy_refusals(state, p.ctx.scope) if state is not None else []
    keys = sorted(k for k in (state or {}).get("state", {}) if k.count(".") == 2)
    vols = [
        f"{name}: {len(files)} files to archive from {v}"
        for name, (v, files) in pa.lost(p, state is not None).items()
    ]
    tables = [f"{p.ctx.scope.names.table('signals', t)}: export as JSON lines" for t in pa.TABLES]
    lines = [
        f"# dev proof plan, {p.ctx.now().isoformat()}", "",
        "## Refusals", *([f"- {b}" for b in bad] or ["- none"]), "",
        "## Present now (dev_ / dev-)", *[f"- {x}" for x in destroy.residue(p.ctx, tidy=False)], "",
        "## 1. Pause every dev schedule, wait for active runs, archive, list again", "",
        *[f"- {v}" for v in vols], *[f"- {x}" for x in tables], "",
        f"## 2. Destroy: {len(keys)} deployed resources", *[f"- {k}" for k in keys],
        *[f"- genie space {s}" for s in genie.found(p.ctx, tidy=False)], "",
        "## 3. Bootstrap", f"    {' '.join(bootstrap_argv(p))}", "",
        "## 4. Verify, then verify-agent after the next :40", "",
    ]  # fmt: skip
    p.write("plan.md", "\n".join(lines))
    p.say(f"plan written to {p.out / 'plan.md'}; {len(bad)} refusals")
    return 2 if bad else 0


EARLIER_RUN = ("archive", "before.txt", "bootstrap.json")
COMPLETE = "archive.txt"  # written once the archive and its recheck have passed


def run(p: Proof, resume: bool = False) -> int:
    bad = refusals(p)
    if resume and not (p.out / COMPLETE).is_file():
        bad.append(f"{p.out} holds no complete archive ({COMPLETE}); nothing to resume")
    elif resume:
        bad += record.resume_refusals(p)
    bad += _pending_refusal(p)
    used = [x for x in EARLIER_RUN if (p.out / x).exists()]
    if used and not resume:
        bad.append(
            f"{p.out} holds an earlier run ({', '.join(used)}); resume it with --resume, "
            "or pass a new --out while dev is still deployed"
        )
    if bad:
        raise Stop(2, "; ".join(bad))
    s = p.ctx.scope
    try:
        with lock.held(lock.path(p.ctx.state_dir, s.target, s.prefix)) as fd, _signals_raise():
            return _locked(p, fd, resume)
    except Busy as err:
        raise Stop(75, f"{err}; nothing paused, archived or destroyed") from err


def _step(p: Proof, name: str, lines: list[str]) -> None:
    p.write(f"{name}.txt", "\n".join(lines) + "\n")
    for x in lines:
        p.say(f"{name}: {x}")


class Signalled(BaseException):
    """SIGTERM or SIGHUP (a closed terminal), raised so the run prints its way back as on Ctrl-C."""


@contextlib.contextmanager
def _signals_raise() -> Iterator[None]:
    def raise_it(signum: int, _frame: object) -> None:
        raise Signalled(signal.Signals(signum).name)

    before = {sig: signal.signal(sig, raise_it) for sig in (signal.SIGTERM, signal.SIGHUP)}
    try:
        yield
    finally:
        for sig, handler in before.items():
            signal.signal(sig, handler)


def way_back(p: Proof) -> str:
    """The one command that finishes what a stopped run left: during the destroy, this run resumed;
    from the bootstrap on, `finish`, which bootstraps again and keeps the record; once the archive
    is complete, bootstrap with its ledger, which it restores only where absent; before that, a
    bare bootstrap to unpause dev and load the agent."""
    s = p.ctx.scope
    if p.destroying and not p.bootstrapping:
        return (
            f"{p.python} -m weather_signal.deploy.proof run --yes --resume --profile "
            f"{s.profile} --seed-from {p.seed_from} --out {p.out}"
        )
    if p.bootstrapping:
        return (
            f"{p.python} -m weather_signal.deploy.proof finish --profile {s.profile} "
            f"--seed-from {p.seed_from} --out {p.out}"
        )
    if (p.out / record.MANIFEST).is_file():
        return " ".join(bootstrap_argv(p))
    exe = Path(p.python).parent / "weather-signal"
    return f"{exe} bootstrap --profile {s.profile} --target dev"


def _pending_refusal(p: Proof) -> list[str]:
    """A destroy began under another --out and its bootstrap has not exited 0: that run is the
    only recovery, since only its folder holds the ledger."""
    other = record.pending(p)
    if other is None or other == p.out:
        return []
    q = replace(p, out=other, destroying=True, bootstrapping=record.bootstrap_began_in(other))
    return [f"the run in {other} began a destroy and has not finished; To finish: {way_back(q)}"]


def _locked(p: Proof, fd: int, resume: bool) -> int:
    """The run under bootstrap's lock, which the bootstrap it starts inherits."""
    state = _guard_state(p)
    left = resource_keys(state) if state is not None else []
    if resume and state is not None and (bad := record.may_finish_destroy(p, left)):
        # Resume destroys only what is left of the destroy this run began; a dev deployed
        # again may hold claims its archive lacks.
        way = way_back(replace(p, bootstrapping=record.bootstrap_began_in(p.out)))
        raise Stop(2, f"dev is deployed (bundle state present), so {p.out} cannot be resumed: "
                      f"{'; '.join(bad)}. To finish: {way}")  # fmt: skip
    if not resume and state is None:
        raise Stop(2, "dev has no deployed state, so there is no ledger left to archive; rerun with "
                      "--resume and the --out of the run that destroyed it")  # fmt: skip
    p.destroying = resume
    try:
        if not resume:
            _archive_then_destroy(p)
        elif record.was_destroyed(p) and not destroy.residue(p.ctx):
            _step(p, "resume", ["destroy: recorded complete in run.json, no dev residue; skipped"])
        else:
            _step(p, "resume", teardown(p, destroy_state=state is not None))
            record.destroyed(p)
        p.bootstrapping = True
        record.bootstrap_began(p)
    except BaseException as err:  # Ctrl-C, SIGTERM, SIGHUP and a probe that does not answer
        if p.paused or p.destroying:
            p.say(f"stopped ({type(err).__name__}); dev is left part-way. To finish: {way_back(p)}")
        raise
    return _bootstrap_then_verify(p, fd)


def finish(p: Proof) -> int:
    """Bootstrap again for a run whose bootstrap began, under bootstrap's lock. The earlier
    attempt's record stays as `bootstrap-<n>.*`; `bootstrap.*` and verify are this attempt's."""
    bad = refusals(p) + record.resume_refusals(p) + _pending_refusal(p)
    if not record.bootstrap_began_in(p.out):
        bad.append(f"{p.out} records no bootstrap; finish it with run --yes --resume")
    if bad:
        raise Stop(2, "; ".join(bad))
    p.destroying = p.bootstrapping = True
    s = p.ctx.scope
    try:
        with lock.held(lock.path(p.ctx.state_dir, s.target, s.prefix)) as fd, _signals_raise():
            return _bootstrap_then_verify(p, fd)
    except Busy as err:
        raise Stop(75, f"{err}; nothing bootstrapped") from err


def _bootstrap_then_verify(p: Proof, fd: int) -> int:
    try:
        rc = bootstrap(p, fd)
    except BaseException as err:
        p.say(f"stopped ({type(err).__name__}); dev is left part-way. To finish: {way_back(p)}")
        raise
    if rc != 0:
        # Verify polls tables a failed bootstrap never built; report the stop instead.
        return proof_verify.bootstrap_failed(p, rc, way_back(p))
    p.say("bootstrap: exit 0")
    return proof_verify.verify(p)


def _archive_then_destroy(p: Proof) -> None:
    pa.session(p)  # before anything is paused
    p.write("before.txt", "\n".join(destroy.residue(p.ctx)) + "\n")
    p.paused = True
    _step(p, "pause", [*pa.pause(p), *pa.drain(p), *pa.unload_agent(p)])
    got, lines = pa.archive(p, deployed=True)
    _step(p, "archive", [*lines, *pa.unchanged(p, got, deployed=True)])
    record.write(p, got)
    recount = lambda: ["recount: " + x for x in pa.unchanged(p, got, True)]  # noqa: E731
    _step(p, "destroy", teardown(p, True, recount))
    record.destroyed(p)


def _stream(argv: list[str], log: Path, fd: int) -> int:
    """Run bootstrap, its lines to the terminal and to `log` as they come; it inherits the lock."""
    log.parent.mkdir(parents=True, exist_ok=True)
    with (
        log.open("w", encoding="utf-8") as f,
        subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            pass_fds=(fd,),
            env={**os.environ, lock.ENV: str(fd)},
        ) as proc,
    ):
        for line in proc.stdout or ():
            f.write(line)
            f.flush()
            sys.stdout.write(line)
            sys.stdout.flush()
        return proc.wait()


def real_proof(a: argparse.Namespace) -> Proof:
    from weather_signal.deploy import command, proof_outlook
    from weather_signal.ingest.files import Files

    s = scope.build(a.profile, "dev", None, "workspace")
    ns = argparse.Namespace(
        seed_from=a.seed_from,
        skip_app=False,
        warehouse_name=None,
        live=False,
        b_catalog="workspace",
    )
    return Proof(
        ctx=command.real_ctx(s, ns),
        out=a.out,
        seed_from=a.seed_from.expanduser(),
        main=Path.cwd(),
        python=sys.executable,
        read=Files.from_sdk(a.profile).read,
        stream=_stream,
        get=proof_outlook.sdk_get(a.profile),
    )


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m weather_signal.deploy.proof")
    p.add_argument("step", choices=["plan", "run", "finish", "verify", "verify-agent"])
    p.add_argument("--profile", required=True)
    p.add_argument("--seed-from", type=Path, required=True, help="the data folder")
    p.add_argument("--out", type=Path, required=True, help="the evidence folder for this proof")
    p.add_argument("--yes", action="store_true", help="run: confirm the dev destroy")
    p.add_argument("--resume", action="store_true", help="run: finish a stopped run in --out")
    p.add_argument("--wait-min", type=float, default=75.0, help="verify: wait for the score run")
    p.add_argument("--since", type=datetime.fromisoformat, help="verify-agent: runs from (UTC)")
    return p


def main(
    argv: list[str] | None = None, make: Callable[[argparse.Namespace], Proof] = real_proof
) -> int:
    a = parser().parse_args(argv)
    if a.step == "run" and not a.yes:
        sys.stdout.write("proof: run destroys dev; pass --yes (plan shows what it removes)\n")
        return 2
    p = make(a)
    steps: dict[str, Callable[[Proof], int]] = {
        "plan": plan,
        "run": lambda q: run(q, a.resume),
        "finish": finish,
        "verify": lambda q: proof_verify.verify(q, wait_s=a.wait_min * 60),
        "verify-agent": lambda q: proof_verify.verify_agent(q, a.since),
    }
    try:
        return steps[a.step](p)
    except (Stop, Unknown) as err:
        p.say(f"stopped: {err}")
        return err.code if isinstance(err, Stop) else 1


if __name__ == "__main__":
    raise SystemExit(main())
