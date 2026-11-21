"""Top-50 MSAs by Census V2025 estimate, principal-city point from the 2024 Places Gazetteer."""

import csv
import sys
from pathlib import Path

d = Path(sys.argv[1])
rows = list(csv.DictReader(open(d / "cbsa-est2025.csv", encoding="latin-1")))
msa = [r for r in rows if r["LSAD"] == "Metropolitan Statistical Area"]
msa.sort(key=lambda r: -int(r["POPESTIMATE2025"]))
top = msa[:50]
places = {}
with open(d / "2024_Gaz_place_national/2024_Gaz_place_national.txt", encoding="utf-8") as f:
    rd = csv.reader(f, delimiter="\t")
    hdr = [h.strip() for h in next(rd)]
    for r in rd:
        r = dict(zip(hdr, [x.strip() for x in r], strict=False))
        nm = r["NAME"]
        for suf in (
            " city",
            " town",
            " village",
            " municipality",
            " CDP",
            " urban county",
            " city and borough",
            " consolidated government (balance)",
            " metro government (balance)",
            " metropolitan government (balance)",
            " unified government (balance)",
            " city (balance)",
        ):
            if nm.endswith(suf):
                nm = nm[: -len(suf)]
                break
        places.setdefault((nm, r["USPS"]), (float(r["INTPTLAT"]), float(r["INTPTLONG"]), r["NAME"]))
total = sum(int(r["POPESTIMATE2025"]) for r in top)
out = csv.writer(open("metros_top50.csv", "w", newline=""))
out.writerow(
    [
        "rank",
        "cbsa_code",
        "cbsa_name",
        "pop_2025",
        "weight",
        "point_place",
        "state",
        "latitude",
        "longitude",
    ]
)
for i, r in enumerate(top, 1):
    name = r["NAME"]
    city = name.split(",")[0].split("-")[0].strip()
    st = name.split(",")[1].strip().split("-")[0]
    city = {
        "Nashville": "Nashville-Davidson",
        "Louisville/Jefferson County": "Louisville/Jefferson County",
    }.get(city, city)
    p = places.get((city, st))
    if p is None:
        p = (None, None, "")
    out.writerow(
        [
            i,
            r["CBSA"],
            name,
            r["POPESTIMATE2025"],
            f"{int(r['POPESTIMATE2025']) / total:.6f}",
            p[2],
            st,
            p[0],
            p[1],
        ]
    )
