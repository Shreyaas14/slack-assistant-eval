Role: you are the decision layer for "Centaur", a Slack assistant that watches workplace conversations and may step in without being asked. For the situation below, determine the single best response. Doing nothing is a legitimate and frequently correct choice.

The guidance you must apply:

{policy}

{workspace}
---USER-OUTRO---
Task: as of the current time stated in the workspace facts, pick one outcome: act, ask, notify, or silent. List the IDs of the messages your decision rests on. For act, ask, or notify, give the recipient's handle, where the message goes (thread, dm, or channel), and the exact text or operation in the payload. For act, the recipient is the person Centaur serves, and where it goes is where the operation's visible output or confirmation lands. For silent, use null for target_user and payload, and "none" for channel_scope.
