# Dashboard UI review — 29 October 2026

## Scope and evidence

Reviewed the shared dashboard's desktop Run and History views, the template,
and the server's stage data. Exercised the existing rendering functions locally
without submitting a request or making a GitHub change. No application behavior
was changed during this review.

The live 390px browser check could not be completed: browser automation repeatedly
lost its window or returned capture errors. Mobile observations below come from
the responsive CSS and still need a real viewport check. Accessibility observations
are a manual review, not a WCAG conformance certification.

Keep the current paper/ink palette, fonts, square corners and simple page order.
The most pressing issues are accurate state, review context and the team handoff.

## Fix before the team demo

### 1. Do not announce check results before there is a result

**Evidence:** `gauntletHtml()` treats an empty violation list as four passes,
including when `rounds` is empty and the stage is still Gauntlet. A local call with
no completed round rendered four `PASS` labels. After a fix, it can also display
the previous draft's results while checking a new draft.

**Change:** Use Waiting/Checking until a completed scan record exists for the
current draft. Show Pass/Fail only from that record. A tool failure should be
explained as a check that could not complete, rather than a manifest policy failure.
Do not invent individual checker progress when the server only supplies a complete
round's results.

**Acceptance:** A draft with no recorded results shows no passes. Results identify
which draft they describe.

### 2. Give blocked, declined, escalated and merged states different meanings

**Evidence:** `finishedHtml()` maps every non-passed terminal status to ESCALATED.
A rejected run did not display its `rejection_reason` in the rendering check.
The headline remains PASSED even when the PR approval record changes.

**Change:** Use distinct, plain-language states, for example:

- Blocked by input filter — with the real reason and what the user can change.
- Plan declined — no files generated.
- Needs manual attention — with the escalation reason.
- Checks passed · awaiting admin review.
- Merged — with the recorded reviewer and time.
- Declined — with the recorded reason.

Keep checks, PR review and deployment status separate. Retain the explicit NOT
BUILT deployment label; do not suggest that a merged PR has been deployed.

### 3. Show an accurate fix diff and the rule that caused it

**Evidence:** `changedLines()` compares lines by position. Inserting one line into
three lines rendered two removals and three additions. `fixHtml()` selects only
one changed file and uses the newest violation rather than reliably associating
the displayed edit with the violation that caused it. It can compare older round
snapshots while the current fix is still being generated.

**Change:** Build actual diff hunks from the correct before/after files. The
existing `ui_data.diff_rows()` already uses sequence matching. List every changed
file, retain line numbers, and explain the triggering rule in a plain sentence.
Show “Applying a fix” while the corrected files are not available yet.

**Acceptance:** One inserted line appears as one addition. The file, draft pair,
rule and visible edit agree.

### 4. Make the teammate-to-admin handoff visible in History

**Evidence:** The live History view shows repeated PASSED buttons followed by
entire prompts and request IDs. It does not show the requester, time, target
namespace or PR review state. It is difficult to find Kavya's request or tell
which run needs the admin's attention.

**Change:** Give each run a short request title and real metadata: requested by,
namespace, started time, current state, and PR/review state. Add an explicit Open
run action and an Awaiting my review filter for admins. Use a compact table on
desktop and the same fields in stacked cards on mobile. Do not fabricate a
requester for older logs that have no ownership record.

### 5. Make the two approvals and the merge consequence explicit

**Evidence:** The plan button says only Approve. The final merge action immediately
posts to `/api/pr/review`; the panel contains a PR URL but little review context.

**Change:** Rename the first action Approve plan. Keep Approve and merge for the
admin action. Before merging, show the request, author, repository/target branch,
namespace and changed files, with a Review diff on GitHub link. Include a clear
confirmation of the exact PR being merged and disable review buttons while the
decision is processing. Use only data the server or GitHub actually supplies.

### 6. Fix host-only links in the shared dashboard

**Evidence:** Grafana and Streamlit links point to `localhost:3000` and
`localhost:8501`. For a teammate, these refer to her own computer.

**Change:** Use configured accessible addresses, or label these services as only
available on the host Mac. Show an unavailable/configuration explanation rather
than a link that appears broken. Their access controls remain independent.

## Presentation and accessibility improvements

- **Notifications:** Replace the club-suit glyph with an actual outline bell.
  Announce the unread count, expose the expanded/collapsed state, and support
  Escape to close. Present human event labels and readable local times.
- **Pipeline:** Remove the arrow after Deploy. The stage is followed by the SVG
  and loop-label elements, so the current `:not(:last-child)` selector also
  matches the final stage. Verify loop alignment at every breakpoint; the
  721–980px layout currently hides the loop entirely.
- **Mobile:** The strip has a 760px minimum width, so a 390px viewport requires
  horizontal scrolling. Make that interaction discoverable and keep the current
  stage easy to find. Check header wrapping, long PR links and member forms at
  390px. Increase tiny 9–10px stage notes and secondary labels for readability.
  Horizontal scrolling for this diagram is not by itself a WCAG failure.
- **Tabs:** Add selected-state and tab/panel relationships to the accessibility
  tree and implement the keyboard behavior expected of tabs. Keep a visible
  focus indicator throughout the interface.
- **Live updates:** `pollRun()` may replace the live panel every 700ms, even
  while awaiting approval. Preserve focus and update only changed content.
  Announce concise status changes instead of repeatedly announcing a whole panel.
- **Connection feedback:** Display when live updates are reconnecting or a
  refresh failed. The current polling error path is silent. Show measured
  elapsed time during slow generation; do not display an invented percentage.
- **Team settings:** Give admins a direct Manage members link into the existing
  About panel. Keep Run, History and About as the three main navigation choices.
- **Role copy:** Replace the long permissions paragraph with a compact role label
  and one relevant next action. Clearly distinguish “approve a plan” from
  “approve and merge a pull request.”

## Standards used

The accessibility reference was [W3C WCAG 2.2](https://www.w3.org/TR/WCAG22/),
particularly focus visibility, labels/instructions, status messages and target
size. Its AA target-size criterion is 24×24 CSS pixels with exceptions; larger
touch areas are a usability recommendation, not a claim that 44px is universally
required for AA. Typography size alone does not determine WCAG compliance.

The status, diff, review and history recommendations address correctness and
common usability expectations for a collaborative operations console. They are
not presented as a formal industry certification.
