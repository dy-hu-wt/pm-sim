# Support Inbox Move

Poppy is moving customer support from an old shared inbox to a new help desk by Thursday afternoon. The switch only works if two separate streams are ready.

![Support Inbox Move Week](../../docs/assets/support-inbox-move-calendar.svg)

## Characters

- Nora, Support Lead: knows the new help desk is missing the saved replies the team uses every day.
- Felix, Infrastructure Engineer: knows VIP customer emails may still route to the old inbox.

## Streams

### 1. Saved Replies

Problem: the old inbox has saved replies, but the new help desk did not import them.

Deadline: Thursday 12:00 PM for support-team readiness.

Owner to talk to: Nora.

Valid solutions:

- Ask Nora about saved replies, templates, support readiness, or old inbox backup.
- Move the top 12 saved replies before Thursday.
- Keep the old inbox read-only for one week as backup.
- Email Nora written wording the support team can follow.

Bad path:

- Say the new help desk is ready without moving saved replies.
- Close the old inbox immediately.

### 2. VIP Email Routing

Problem: VIP customer aliases bypass the default forwarding rule, so urgent emails may keep landing in the old inbox.

Deadline: Thursday 12:00 PM for routing readiness.

Owner to talk to: Felix.

Valid solutions:

- Ask Felix about VIP email routing, forwarding, aliases, or testing.
- Add VIP aliases to the forwarding allowlist.
- Send a test email to confirm VIP messages reach the new help desk.
- Email Felix the final routing plan.

Bad path:

- Assume normal forwarding covers VIP aliases.
- Skip the VIP routing test.

## Causal Path

The PM needs both streams:

1. Discover Nora's saved-reply risk.
2. Discover Felix's VIP-routing risk.
3. Write the Thursday Inbox Move Plan.
4. Email Nora and Felix their parts of the plan.
5. Reach Thursday with both written updates complete.

## Guaranteed Events

- Tuesday 10:00 AM: Nora reminds the PM if saved replies have not been handled.
- Tuesday 11:00 AM: Felix reminds the PM if VIP routing has not been handled.
- Thursday 9:30 AM: Nora nudges again if she lacks written support-team wording.
- Thursday 10:00 AM: Felix nudges again if he lacks the routing note.
- Thursday 3:00 PM: the inbox move deadline is evaluated.

## Starts Visible vs Hidden

Visible:

- The support inbox move brief.
- The empty Thursday Inbox Move Plan.
- The public blocker that there is no written move plan.
- Initial messages from Nora and Felix.

Hidden until discovered:

- Saved replies are missing from the new help desk.
- Moving the top 12 replies is enough if the old inbox stays read-only.
- VIP aliases bypass the default forwarding rule.
- A tested VIP forwarding allowlist fixes the routing gap.

Derived by agent action:

- The final inbox move plan.
- Nora's written support-team update.
- Felix's written VIP-routing update.

## Scoring

Saved replies (30 points):

- Discover Nora's saved-reply risk.
- Send Nora written support-team wording.

VIP routing (30 points):

- Discover Felix's VIP-routing risk.
- Send Felix written routing wording.

Written plan (25 points):

- Update the Thursday Inbox Move Plan with saved replies, VIP routing, old inbox backup, and both owners.

Final readiness (15 points):

- Both owners have written updates before the Thursday deadline.

## Best Path

```bash
pm-sim reset --scenario scenarios/support_inbox_move
pm-sim read-doc inbox_move_brief
pm-sim send-chat nora "For the support inbox move, are saved replies or old inbox backup a risk before Thursday?"
pm-sim advance-time until_next_event
pm-sim send-chat felix "For the new help desk, do VIP email aliases route correctly, and do we need a forwarding test?"
pm-sim advance-time until_next_event
pm-sim update-doc inbox_move_plan "Thursday inbox move plan: move the top 12 saved replies to the new help desk before Thursday. VIP forwarding is tested by adding VIP aliases to the forwarding rule and sending a test email. The old inbox stays read-only for one week as backup. Nora owns saved replies and Felix owns VIP routing; Nora and Felix are aligned."
pm-sim send-email nora "Support inbox move saved replies" "Support team can use this wording: the top 12 saved replies move to the new help desk before Thursday, and the old inbox stays read-only for one week as backup."
pm-sim send-email felix "Support inbox move VIP routing" "VIP aliases are on the forwarding allowlist, VIP forwarding is tested to the new help desk, and the old inbox stays open as backup for one week."
pm-sim advance-time to:2026-06-25T15:00:00
pm-sim evaluate --explain
```
