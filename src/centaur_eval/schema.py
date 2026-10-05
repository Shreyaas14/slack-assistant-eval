"""Pydantic models for scenarios, labels, model decisions, and traces.

Labels live alongside each scenario but are never rendered to the model.
"""

from __future__ import annotations

from typing import Literal, get_args

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

Action = Literal["act", "ask", "notify", "silent"]
ChannelScope = Literal["thread", "dm", "channel", "none"]
Bucket = Literal["routine", "ambiguous", "permission", "annoying", "timing"]
TriggerKind = Literal["mention", "message", "bot_message", "timer", "sweep"]

ACTIONS: tuple[str, ...] = get_args(Action)
BUCKETS: tuple[str, ...] = get_args(Bucket)


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")  # stale YAML keys fail validation loudly


class Reaction(Strict):
    emoji: str
    users: list[str]
    at: AwareDatetime | None = None  # when added; if unset, UNTIMED_REACTION_DELAY after the message


class FileRef(Strict):
    name: str
    kind: Literal["deck", "doc", "sheet", "log", "image", "other"] = "other"
    doc_id: str | None = None


class Message(Strict):
    id: str
    ts: AwareDatetime
    user: str
    channel: str
    thread_parent: str | None = None  # id of the thread's root message (Slack thread_ts)
    text: str
    reactions: list[Reaction] = []
    files: list[FileRef] = []
    is_bot: bool = False
    edited: bool = False  # text is the post-edit version, as a bot's message store would hold it


class User(Strict):
    handle: str
    display_name: str
    title: str
    org: Literal["internal", "external"] = "internal"  # external: another org, via Slack Connect
    account_type: Literal["member", "guest", "contractor", "external", "bot"] = "member"
    timezone: str = "America/Los_Angeles"
    status: str | None = None
    owns_channels: list[str] = []


class Contact(Strict):
    """Someone known from email or CRM who is not in Slack (cannot be messaged by Centaur)."""

    name: str
    email: str
    org: str
    title: str | None = None


class Channel(Strict):
    name: str
    type: Literal["public", "private", "dm", "group_dm", "shared"]
    member_count: int
    external_orgs: list[str] = []
    purpose: str | None = None
    centaur_member: bool = True  # a bot only receives events from conversations it belongs to


class LinkedDoc(Strict):
    id: str
    title: str
    owner: str
    sharing: Literal["restricted", "team", "company", "external_link"]
    shared_with: list[str] = []  # explicit access list (handles or #channels) for restricted docs
    status: Literal["draft", "final"] = "final"
    excerpt: str


class CalendarEvent(Strict):
    id: str
    title: str
    start: AwareDatetime
    end: AwareDatetime
    organizer: str
    attendees: list[str]
    recurring: str | None = None
    attachments: list[str] = []
    external: bool = False


class Delegation(Strict):
    from_user: str
    instruction: str
    given_at: AwareDatetime
    msg_id: str | None = None


class AssistantScope(Strict):
    # Capabilities live in prompts/workspace.md, identical for every scenario, so none can telegraph the answer.
    standing_delegations: list[Delegation] = []


class Trigger(Strict):
    """Why Centaur's runtime invoked the decision model at `now`."""

    kind: TriggerKind
    msg_id: str | None = None  # the message event that woke Centaur; None for timers and sweeps
    detail: str | None = None  # neutral runtime note, e.g. "15 min before ev1"


class Context(Strict):
    now: AwareDatetime
    trigger: Trigger
    focal_channel: str
    users: list[User]
    contacts: list[Contact] = []
    channels: list[Channel]
    docs: list[LinkedDoc] = []
    calendar: list[CalendarEvent] = []
    assistant: AssistantScope = AssistantScope()
    visible_elsewhere: list[Message] = []  # recent messages from other conversations Centaur belongs to


class Outcome(Strict):
    action: Action
    targets: list[str] = []  # acceptable target handles; [] for silent
    channels: list[ChannelScope] = ["none"]
    # Case-insensitive regexes for full credit; `validate` checks them against a known-good reference_payload.
    payload_must: list[str] = []
    reference_payload: str | None = None

    @model_validator(mode="after")
    def _silent_has_no_target(self) -> Outcome:
        if self.action == "silent":
            if self.targets or self.channels != ["none"] or self.payload_must:
                raise ValueError("silent outcome has no targets, channels, or payload checks")
        else:
            if not self.targets:
                raise ValueError(f"{self.action} outcome needs at least one acceptable target")
            if "none" in self.channels:
                raise ValueError(f"{self.action} outcome cannot have channel 'none'")
        return self


class DecisiveCue(Strict):
    msg_ids: list[str] = []
    context_path: str | None = None
    position: Literal["last", "mid", "early", "context"]
    description: str


class Flags(Strict):
    requires_authorization: bool = False  # acting would exceed someone's authority
    private_content: bool = False  # content must not reach a wider audience
    irreversible: bool = False  # external send, deletion, payment-triggering forward
    ask_is_wrong: bool = False  # info already supplied or step trivially delegated
    lookalike: bool = False  # gold silent despite surface trigger features


class Label(Strict):
    gold: Outcome
    acceptable: list[Outcome] = []  # defensible alternatives; each costs ACCEPTABLE_COST
    ambiguity: Literal["clear", "contested"] = "clear"
    rationale: str
    why_not: dict[str, str]
    decisive_cue: DecisiveCue
    principle_refs: list[str]
    # A match in a non-silent payload is a G-LEAK violation, except in a DM to a leak_ok_reader (e.g. its author).
    payload_must_not: list[str] = []
    leak_ok_readers: list[str] = []

    @property
    def credited(self) -> list[Outcome]:
        return [self.gold, *self.acceptable]


class Checkpoint(Strict):
    """Another invocation of the same scenario at a different moment, labeled on its own."""

    id: str  # short suffix, e.g. "early" -> item id "S12@early"
    title: str | None = None  # set when the scenario title does not hold at this moment
    now: AwareDatetime
    trigger: Trigger
    label: Label


class Scenario(Strict):
    id: str
    slug: str
    title: str
    family_id: str | None = None
    variant_of: str | None = None
    perturbation: str | None = None
    bucket: Bucket
    distractor_msg_ids: list[str] = []
    context: Context
    transcript: list[Message]
    label: Label
    flags: Flags = Flags()
    checkpoints: list[Checkpoint] = []

    def all_message_ids(self) -> set[str]:
        return {m.id for m in self.transcript} | {m.id for m in self.context.visible_elsewhere}


class Decision(BaseModel):
    """What the system under test returns. Field order = intended generation order."""

    evidence_msg_ids: list[str] = Field(description="IDs of the messages that drove the decision.")
    rationale: str = Field(description="At most 3 sentences explaining the decision.")
    action: Action
    target_user: str | None = Field(
        description="Handle of the person the message is for (for act: the person served); null iff silent."
    )
    channel_scope: ChannelScope = Field(description="thread, dm, or channel; 'none' iff silent.")
    payload: str | None = Field(
        description="The exact message Centaur would send or the operation it would perform; null iff silent."
    )

    @model_validator(mode="after")
    def _consistent(self) -> Decision:
        if self.action == "silent":
            if self.target_user or self.channel_scope != "none":
                raise ValueError("silent decision must have target_user=null and channel_scope='none'")
        elif self.action != "act" and (not self.target_user or self.channel_scope == "none"):
            # a bare `act` (no target) stays valid, scored as a delivery miss, so gates still apply
            raise ValueError(f"{self.action} decision needs a target_user and a channel_scope")
        return self


ParseStatus = Literal["ok", "invalid_json", "schema_error", "refusal", "api_error"]


class Trace(BaseModel):
    run_id: str
    item_id: str  # scenario id, or "<scenario>@<checkpoint>" for another moment
    epoch: int
    provider: str
    model: str
    params: dict = {}
    system_prompt_sha: str
    prompt_sha: str
    user_prompt: str
    raw_response: str | None
    parse_status: ParseStatus
    decision: Decision | None
    error: str | None = None
    latency_ms: int | None = None
    usage: dict = {}
    ts: AwareDatetime
