"""A turn's reply text as the shopper sees it: the parts the model writes between tool calls
joined with a paragraph break, and a part that only repeats an earlier one dropped.

Live (search eval): the reply read "Let me check our catalog.Let me check our catalog.
Here are..." - the text before a tool call and the text after it, glued with no space, the
second starting with a repeat of the first. chat.html already added the break; every other
client (the widget, the evals, the API) got the glued text. Now the host sends it right.
"""

from __future__ import annotations

# A repeat shorter than this is a coincidence ("Sure." / "Sure, here..."), not a duplicate.
MIN_REPEAT = 25


class TextFlow:
    def __init__(self) -> None:
        self._earlier: list[str] = []  # finished parts, stripped
        self._part = ""  # the part being written, as received
        self._held = ""  # received but not sent: so far a repeat of an earlier part
        self._checked = False  # this part has been cleared as new text
        self._started = False  # this part has sent something
        self._sent_any = False
        self._ends_space = True
        self._last = " "

    def tool(self) -> None:
        """A tool call: the next text is a new part."""
        if self._part.strip():
            self._earlier.append(self._part.strip())
        self._part, self._held, self._checked, self._started = "", "", False, False

    def feed(self, text: str) -> str:
        """The text to send now for a delta just received ('' while holding)."""
        self._part += text
        if self._checked:
            return self._send(text)
        self._held += text
        head = self._held.lstrip()
        if not head:
            return ""
        long_earlier = [e for e in self._earlier if len(e) >= MIN_REPEAT]
        if any(e.startswith(head) for e in long_earlier):
            return ""  # so far a repeat of an earlier part: keep holding
        self._checked, self._held = True, ""
        for e in long_earlier:
            if head.startswith(e):  # a whole earlier part repeated, then new text
                return self._send(head[len(e) :].lstrip())
        return self._send(head)

    def end(self) -> str:
        """At the end of the turn: held text that is a repeat of an earlier part is
        dropped; anything else held is sent."""
        head, self._held = self._held.lstrip(), ""
        if not head or any(e.startswith(head) for e in self._earlier if len(e) >= MIN_REPEAT):
            return ""
        return self._send(head)

    def _send(self, text: str) -> str:
        if not text:
            return ""
        if not self._started:
            self._started = True
            if self._sent_any and not self._ends_space:
                text = "\n\n" + text.lstrip()
        elif self._sent_any and self._last in ".!?" and text[:1].isupper():
            # Two text blocks with no tool call between them arrive glued ("right now.No
            # smartwatches", eval item 38): inside one block the model always puts a space
            # after a full stop, so a capital straight after one starts a new block.
            text = "\n\n" + text
        self._sent_any = True
        self._ends_space = text[-1].isspace()
        self._last = text[-1]
        return text
