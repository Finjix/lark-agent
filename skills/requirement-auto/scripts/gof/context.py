"""What the agent needs besides the requirement document to fill a request in by itself.

The target sheet, the next material number and the dropdown options. Never earlier rows or earlier batches: the agent works from the
document alone. Read-only.
"""

from __future__ import annotations

from typing import Optional

from .feishu import OPTION_FIELDS, Bridge, _sorted_titles, read_rules, sheets_of, target
from .plan import naming_pattern, row_formula
from .rules import column_options
from .snapshot import Snapshot, choose_sheet, collect_ids
from .util import canonical, need


def context(bridge: Bridge, snapshot: Snapshot, worksheet: Optional[str] = None) -> dict:
    bridge.doctor()
    sheet, tables, _ = choose_sheet(snapshot, {"worksheet": worksheet} if worksheet else {}, None)
    cols = tables[sheet]
    token = target(bridge)
    metadata = sheets_of(bridge, token)
    need(canonical(_sorted_titles(metadata)) == canonical(snapshot.sheet_names), "the API's list of tabs does not match the XLSX: export a new snapshot first")
    meta = next(s for s in metadata if s.get("title") == sheet)
    rules = read_rules(bridge, token, [meta])
    last = snapshot.last[sheet]
    options_of = {field: column_options(rules, meta["sheet_id"], cols[field], last) if field in cols else [] for field in OPTION_FIELDS.values()}
    following = max([0, *collect_ids(snapshot, tables).keys()]) + 1
    # How a new row will be named, in the sheet's own words.
    naming = naming_pattern(row_formula(snapshot, sheet, cols, last + 1, False), last + 1, cols)
    return {"status": "context", "worksheet": sheet, "sheet_id": meta["sheet_id"], "next_number": following, "naming": naming, "options": options_of}
