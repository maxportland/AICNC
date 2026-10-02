"""
Confirmation gate for machine actions proposed by the AI Assistant.

Nothing that moves the machine runs until the user confirms it, either with
the Confirm button or by saying/typing "yes". Anything else cancels it, and a
pending action expires on its own after a timeout.
"""

import re
from typing import Callable, Optional

from PyQt5.QtCore import QTimer


WAKE_PHRASE_RE = re.compile(r'^\s*(?:hey|hi|hello|okay|ok)[\s,]+milo\b[\s,.!?]*', re.IGNORECASE)

NO_WORDS = {"no", "nope", "cancel", "stop", "abort", "don't", "dont", "never", "nevermind", "negative"}
YES_WORDS = {"yes", "yeah", "yep", "yup", "confirm", "confirmed", "go", "execute", "affirmative",
             "ok", "okay", "sure", "do", "it", "ahead", "please"}
# A reply must contain at least one of these, so "it" or "please" alone doesn't confirm
STRONG_YES_WORDS = {"yes", "yeah", "yep", "yup", "confirm", "confirmed", "go", "execute",
                    "affirmative", "ok", "okay", "sure", "do"}
# Words that can pad a refusal ("no, don't do that") without making it a new request
NO_FILLER = {"do", "it", "that", "this", "please", "thanks", "thank", "you", "mind", "not", "i", "said", "way"}


def strip_wake_phrase(text: str) -> str:
    """Remove a leading 'Hey Milo' that the transcription picked up"""
    return WAKE_PHRASE_RE.sub("", text).strip()


def classify_reply(text: str) -> Optional[str]:
    """Return 'yes', 'no', or None if the text isn't a confirmation reply"""
    words = re.sub(r"[^a-z' ]", " ", strip_wake_phrase(text).lower()).split()
    if not words:
        return None
    if any(w in NO_WORDS for w in words):
        # "No, move Y ten" is a new request, not just a refusal
        if all(w in NO_WORDS or w in NO_FILLER for w in words):
            return "no"
        return None
    if all(w in YES_WORDS for w in words) and any(w in STRONG_YES_WORDS for w in words):
        return "yes"
    return None


# Filler that can accompany a cancel ("no thanks", "never mind", "forget it")
CANCEL_FILLER = {"mind", "that", "it", "please", "thanks", "thank", "you", "ok", "okay", "forget", "i", "said"}


def is_cancel_reply(text: str) -> bool:
    """True if the whole reply just says cancel ("cancel", "never mind", "no thanks", "forget it").

    Stricter than classify_reply: "No, I meant move Y ten" is an answer, not a cancel.
    """
    words = re.sub(r"[^a-z' ]", " ", strip_wake_phrase(text).lower()).split()
    return (bool(words) and all(w in NO_WORDS or w in CANCEL_FILLER for w in words)
            and any(w in NO_WORDS or w == "forget" for w in words))


# "Let's start a new chat", "clear context", "new conversation please", "reset the chat", "start over"
_NEW_CONVERSATION_RE = re.compile(
    r"^(?:(?:ok|okay|alright|so|now|please|milo|can we|could we|let's|lets|let us|i want to|i'd like to|"
    r"i would like to|go ahead and)\s+)*"
    r"(?:(?:start|begin|open|make)\s+(?:a\s+|the\s+)?(?:new|fresh|clean)\s+(?:chat|conversation|session|context)"
    r"|(?:a\s+)?(?:new|fresh)\s+(?:chat|conversation|session)"
    r"|(?:clear|reset|wipe|erase)\s+(?:the\s+|our\s+|your\s+|this\s+)?"
    r"(?:context|conversation|chat|history|chat history|conversation history|memory)"
    r"|start\s+(?:over|fresh|from scratch))"
    r"(?:\s+(?:please|now|milo|thanks|thank you))*$")


def is_new_conversation_request(text: str) -> bool:
    """True if the whole request asks to start a new conversation ("Let's start a new chat",
    "Clear context"). The whole request, so "clear the chips off the table" isn't one."""
    words = re.sub(r"[^a-z' ]", " ", strip_wake_phrase(text).lower()).split()
    return bool(_NEW_CONVERSATION_RE.match(" ".join(words)))


class ActionConfirmation:
    """Holds at most one proposed machine action until the user confirms or cancels it.

    The gate has no UI of its own: proposed_callback(action) tells the screen to show the
    confirmation, and resolved_callback() tells it the action was confirmed, cancelled or
    timed out.
    """

    def __init__(self, widgets=None, execute_callback: Callable[[dict], None] = None, log_callback=None,
                 timeout_seconds: int = 30, resolved_callback: Optional[Callable[[], None]] = None,
                 proposed_callback: Optional[Callable[[dict], None]] = None):
        """
        Args:
            widgets: Unused; kept so older callers keep working
            execute_callback: Called with the action dict when the user confirms
            log_callback: Optional callback function for logging
            timeout_seconds: A pending action is cancelled after this long
            resolved_callback: Called whenever a pending action is confirmed, cancelled or times out
            proposed_callback: Called with the action when one is put up for confirmation
        """
        self.execute_callback = execute_callback or (lambda action: None)
        self.log = log_callback if log_callback else (lambda msg: None)
        self.timeout_seconds = timeout_seconds
        self.resolved_callback = resolved_callback
        self.proposed_callback = proposed_callback
        self.action = None
        self._next_id = 1
        self.timer = QTimer()
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(lambda: self.cancel("Timed out"))

    @property
    def pending(self) -> bool:
        return self.action is not None

    def remaining_seconds(self) -> float:
        """Time left before a pending action expires"""
        if not self.pending or not self.timer.isActive():
            return 0.0
        return max(0.0, self.timer.remainingTime() / 1000.0)

    def propose(self, action: dict):
        """
        Put an action up for confirmation.

        Args:
            action: dict with 'kind' ('mdi', 'home', 'circle', 'power_on', 'power_off', 'run'),
                    'summary', and 'command' for mdi/circle
        """
        if self.action is not None:
            self.cancel("Superseded")
        action["id"] = self._next_id
        self._next_id += 1
        self.action = action
        text = action["summary"]
        if action.get("command"):
            text += f"  [{action['command']}]"
        self.log(f"[CONFIRM] {text} - say 'yes' or press Confirm, 'no' or Cancel to abort.")
        self.timer.start(self.timeout_seconds * 1000)
        if self.proposed_callback is not None:
            self.proposed_callback(action)

    def confirm(self, action_id: Optional[int] = None):
        """Run the pending action. With action_id, only if that is still the pending one, so a
        tap on a card can't confirm a different action that replaced it."""
        if action_id is not None and (self.action is None or self.action.get("id") != action_id):
            self.log("[CONFIRM] That confirmation was replaced by another action; nothing was run.")
            return
        action = self._clear()
        if action is None:
            return
        self.log(f"[CONFIRM] Confirmed: {action['summary']}")
        self.execute_callback(action)

    def cancel(self, reason: str = "Cancelled"):
        """Drop the pending action without running it"""
        action = self._clear()
        if action is not None:
            self.log(f"[CONFIRM] {reason}: {action['summary']} (not executed)")

    def _clear(self) -> Optional[dict]:
        action, self.action = self.action, None
        self.timer.stop()
        if action is not None and self.resolved_callback is not None:
            self.resolved_callback()
        return action
