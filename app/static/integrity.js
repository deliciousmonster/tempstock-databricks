// U2: a call's integrity state at render. Mirrors app/ws_ui/integrity.py; classic script, no import.
(function (root) {
  "use strict";
  var ISO = /^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])T([01]\d|2[0-3]):[0-5]\d:[0-5]\d(\.\d+)?(Z|[+-]\d{2}:[0-5]\d)$/;
  var MAX_AGE = 75 * 60e3, SKEW = 5 * 60e3, MAX_OFFSET_MIN = 18 * 60;
  var LABELS = {
    not_set_up: "unknown: guard not set up",
    held: "held",
    flagged: "flagged",
    unreadable: "unknown: integrity unreadable",
    stale: "unknown: integrity as of {read_at}",
    awaiting: "unknown: awaiting guard check",
    verified: "verified"
  };

  function unreadable() { return new RangeError("unreadable"); }

  function ts(v, required) {
    if (required === undefined) required = true;
    if ((v === null || v === undefined) && !required) return null;
    if (typeof v !== "string" || !ISO.test(v)) throw unreadable();
    if (Number(v.slice(0, 4)) < 1) throw unreadable();
    var t = Date.parse(v);
    if (!Number.isFinite(t)) throw unreadable();
    var m = v.match(/(Z|([+-])(\d{2}):(\d{2}))$/);
    var off = m[1] === "Z" ? 0 : (m[2] === "-" ? -1 : 1) * (Number(m[3]) * 60 + Number(m[4]));
    if (Math.abs(off) > MAX_OFFSET_MIN) throw unreadable();
    // V8 rolls Feb 30 into Mar 2: re-derive the wall-clock fields in the input's own offset.
    var back = new Date(t + off * 60e3).toISOString().slice(0, 19);
    if (back !== v.slice(0, 19)) throw unreadable();
    return t;
  }

  function problems(v) {
    if (v === null || v === undefined || v === "" || (Array.isArray(v) && v.length === 0)) return false;
    if (typeof v === "string" || (Array.isArray(v) && v.every(function (x) { return typeof x === "string"; }))) return true;
    throw unreadable();
  }

  function integrityState(row, now) {
    if (row.guarded_tables !== 2 || row.open_incident === null || row.open_incident === undefined) return "not_set_up";
    if (row.open_incident) return "held";
    var n, readAt, asOf, written, post;
    try {
      if (problems(row.audit_problem)) return "flagged";
      n = ts(now); readAt = ts(row.read_at); asOf = ts(row.as_of);
      written = ts(row.written_at, false); post = ts(row.post_verified_as_of, false);
    } catch (e) { return "unreadable"; }
    var age = n - readAt;
    if (age > MAX_AGE || age < -SKEW) return "stale";
    if (post === null || post <= (written === null ? asOf : written)) return "awaiting";
    return "verified";
  }

  function integrityLabel(key, readAt) { return LABELS[key].replace("{read_at}", String(readAt)); }

  root.integrityState = integrityState;
  root.integrityLabel = integrityLabel;
  if (typeof module !== "undefined" && module.exports) module.exports = { integrityState: integrityState, integrityLabel: integrityLabel };
})(typeof window !== "undefined" ? window : globalThis);
