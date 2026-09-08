# Workspace preferences short-viewport repair

## Contract Routing
Rendered workspace menu containment and keyboard accessibility only. No history storage, GET routing, model inference, or production state changes. Preserve existing appearance tokens and original assertions.

## Root cause and repair
At a 480×320 viewport the original menu occupied y=42 through y=337.203125. This is vertical overflow, not a 320-pixel width failure. The old positioner measured a width inconsistent with the rendered box and only flipped upward when enough space existed; otherwise the bottom could escape.

CSS now sets the width before measurement, limits width/height to the viewport minus 16 pixels, and permits vertical scrolling. JavaScript measures the rendered rectangle and clamps its top position to the viewport while retaining upward anchoring when it fits.

## Executed evidence
Evidence root: `/workspace/history-failure-evidence/post-bd9ff9e1/workspace-prefs-resolution/`.
- `red-original`: original four cases, 3 FAIL / 1 PASS, pytest exit 1.
- `negative-final`: implementation-only reversion, 9 FAIL / 1 PASS, exit 1; restored implementation byte-for-byte.
- `green-final`: 10 PASS / 0 SKIP, exit 0.
- `parent-resume-verified`: parent independently reran the current complete layout module, 10 PASS / 0 SKIP, exit 0.
- Parent independently parsed all 13 `adjacent-*` JUnit files: exactly 68 unique original browser nodeids, all PASS, no missing/extra nodes. This earlier adjacency run precedes final strengthening of the added locale assertions; the current full layout module was rerun afterward.
- Parent aggregate and current source hashes: `parent-resume-audit.json`.

The six added cases cover actual en/de/ru locale switching, widths 320/480, resize through heights 600/320/160 and back, scroll containment, radio arrow navigation, Tab to the hidden-files checkbox, Space activation, wheel access to both ends, and Escape dismissal. The original four test bodies remain unchanged. Existing locale-named tests alone did not prove the locale stayed selected through initialization; the added cases explicitly assert the live locale and translated group label.

## Risks, countermeasures, verification, rollback
- Fixed width may change wrapping: cap to viewport width, measure the final rendered rectangle; original layouts and actual long German/Russian labels are tested.
- Height caps can hide controls: use native vertical scrolling rather than clipping; keyboard and wheel access to the final control are asserted at height 160.
- Placement can become stale on resizing or dynamic content: preserve existing reposition hooks and created-sort support-flip test; resize down/up cases verify containment.
- Geometric success is not comprehensive visual acceptance: screenshots are supplementary; no claim of physical-device, browser-zoom, virtual-keyboard, or every-locale acceptance.
- Prior full pytest gate (15,205 PASS / 81 SKIP at f509dcd6) predates this layout diff and cannot certify this working tree.
- Rollback scope is only this CSS rule, positioner changes and added tests through a reviewed Git revert. Do not delete any historical data, backups or retained evidence.

## Release boundary
Implementation and test execution are verified as above. Independent review and a new frozen full-suite run must be separately recorded before release acceptance. No production deployment or LEGACY read-path switch was performed by this repair.
