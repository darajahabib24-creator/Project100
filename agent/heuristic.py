"""heuristic.py - deterministic policy so the agent works with no model weights.

RulePolicy is a Model that reads the transcript and the tool manifest and emits
the next sensible tool call: look up a file, read it, search, or write a file
named in the goal. It is deliberately dumb and transparent - it is a stand-in,
not a language model - but it makes the whole loop genuinely useful offline.
"""

from __future__ import annotations

import os
import re
from typing import Any

from .core import Model, render
from .search import normalize_query

# Goal phrases that name a file to create. The filename must look like a path
# token (contain a dot) so the literal words "file"/"script" are not captured.
CREATE_RE = re.compile(
    r"\b(?:write|create|make|generate|save)\b[^.\n]*?"
    r"\b([\w][\w./-]*\.[\w]{1,8})\b",
    re.I,
)
# Goal phrases that name a file to read
READ_RE = re.compile(
    r"\b(?:read|open|show|cat|print|inspect|check)\b[^.\n]*?"
    r"\b([\w][\w./-]*\.[\w]{1,8})\b",
    re.I,
)
# Last-resort: any filename-looking token in the goal.
ANY_FILE_RE = re.compile(r"\b([\w][\w./-]*\.[\w]{1,8})\b")
# "remember that X is Y" / "remember X"
REMEMBER_RE = re.compile(r"\bremember\b(?: that)?\s+(.+,?)$", re.I)
# Question-shaped goals: "what is X", "who wrote Y", "find out Z"
QUESTION_RE = re.compile(
    r"^(?:what|who|when|where|why|how|which|whose)\b|"
    r"\b(?:find out|look up|search for|search the web|google|research|"
    r"check the web|what's|whats)\b",
    re.I,
)
# Fix-shaped goals: "fix the bug in x.py", "why does x fail"
FIX_RE = re.compile(
    r"\b(?:fix|debug|solve|resolve|repair|patch)\b|"
    r"\b(?:why does|why is|what's wrong with|error in|failing)\b",
    re.I,
)
# Verify-shaped goals: "verify X", "confirm that Y", "is Z true"
VERIFY_RE = re.compile(
    r"\b(?:verify|confirm|cross-check|cross check|double-check|double check|"
    r"check if|is\s+.+?\s+true|fact check)\b",
    re.I,
)


def _call(tool: str, **args: Any) -> str:
    import json
    return "```json\n" + json.dumps({"tool": tool, "args": args}) + "\n```"


class RulePolicy(Model):
    """Deterministic, explainable next-action policy. No weights, no network."""

    def __init__(self, max_calls: int = 8):
        self.max_calls = max_calls
        self.calls_made = 0

    def _verify_turn(self, goal: str, prompt: str) -> str:
        """Search for the claim, read the top page, then score agreement."""
        turn = self._turn(prompt)
        searched = "[tool:web_search]" in turn
        browsed = "[tool:browse_url]" in turn
        verified = "[tool:verify_answer]" in turn

        if verified:
            return ("Verdict: " + self._verdict_from_transcript(prompt) +
                    ". The task is complete.")

        if not searched:
            return "I will search for sources on this claim.\n" + \
                _call("web_search", query=goal, count=6)

        if not browsed:
            url = self._first_result_url(prompt)
            return "I will read the top source.\n" + \
                _call("browse_url", url=url, max_chars=2500)

        src = self._first_source_block(prompt)
        return "I will score the claim against the source I read.\n" + \
            _call("verify_answer", claim=self._claim_from_goal(goal),
                  sources=src)

    @staticmethod
    def _verdict_from_transcript(prompt: str) -> str:
        m = re.findall(r"\[tool:verify_answer\]\n(.*?)(?=\n\n|\Z)", prompt, re.S)
        if not m:
            return "no verdict was produced"
        block = m[-1]
        conf = re.search(r"^CONFIDENCE:\s*(.+)$", block, re.M)
        claim = re.search(r"^CLAIM:\s*(.+)$", block, re.M)
        parts = []
        if claim:
            parts.append(claim.group(1).strip())
        if conf:
            parts.append("confidence " + conf.group(1).strip())
        return " - ".join(parts) if parts else block.strip()[:160]

    @staticmethod
    def _claim_from_goal(goal: str) -> str:
        m = re.search(r"\b(?:verify|confirm|check if|fact check)\b\s*(?:that\s+)?(.+)",
                      goal, re.I)
        return m.group(1).strip().rstrip(".") if m else goal

    @staticmethod
    def _first_result_url(prompt: str) -> str:
        m = re.search(r"^\d+\.\s+.+\n\s+(https?://\S+)", prompt, re.M)
        if m:
            return m.group(1)
        m = re.search(r"^SOURCE:\s*(https?://\S+)", prompt, re.M)
        return m.group(1) if m else "https://en.wikipedia.org"

    @staticmethod
    def _first_source_block(prompt: str) -> str:
        """The last browse_url page body, as a one-element JSON source list."""
        import json
        m = re.findall(r"\[tool:browse_url\]\n(.*?)(?=\n\n|\Z)", prompt, re.S)
        if not m:
            return json.dumps([{"title": "", "url": "", "text": ""}])
        body = m[-1]
        title = ""
        tm = re.match(r"PAGE:\s*(.*)", body)
        if tm:
            title = tm.group(1).strip()
        url = ""
        um = re.search(r"^URL:\s*(https?://\S+)", body, re.M)
        if um:
            url = um.group(1)
        return json.dumps([{"title": title, "url": url, "text": body[:2500]}])

    def reset_turn(self) -> None:
        """Chat calls this before each question so per-turn state starts clean."""
        self.calls_made = 0

    @staticmethod
    def _turn(prompt: str) -> str:
        """Only the CURRENT question's part of the transcript.

        The prompt embeds the whole history, so a naive `in prompt` check makes
        a persistent chat think it already did this turn's work. Everything
        after the last [user] line belongs to the current question.
        """
        i = prompt.rfind("[user]")
        return prompt[i:] if i >= 0 else prompt

    def _fix_turn(self, goal: str, prompt: str) -> str:
        """Read -> reproduce (run it) -> search the web for the error -> report.

        A rules policy cannot judge which code change is correct, so it stops at
        a grounded diagnosis: the real traceback plus the top web references.
        Attaching a model is what turns this into an actual patch.
        """
        turn = self._turn(prompt)
        searched = "[tool:web_search]" in turn
        ran = "[tool:shell]" in turn
        read_files = "[tool:read_file]" in turn

        m = ANY_FILE_RE.search(goal)
        path = m.group(1) if m else ""

        if path and os.path.isfile(path) and not read_files:
            return "I will read the affected file first.\n" + \
                _call("read_file", path=path)

        if path and read_files and not ran:
            return "I will run it to reproduce the failure.\n" + \
                _call("shell", command=f"python3 {path} 2>&1 | tail -20", cwd=".")

        if ran and not searched:
            err = self._last_shell_output(prompt)
            q = f"{path} {err} fix" if err else f"{goal} fix"
            return "I will search the web for this specific error.\n" + \
                _call("web_search", query=q, count=5)

        if searched:
            err = self._last_shell_output(prompt)
            return ("Diagnosis ready. Reproduced error: " +
                    (err or "see the run output above") +
                    ". Top references were found via web_search. "
                    "A model backend is required to choose the patch safely.")

        return "I will look at the working directory first.\n" + \
            _call("list_dir", path=".")

    @staticmethod
    def _last_shell_output(prompt: str) -> str:
        """Pull the last meaningful line of a shell result out of the transcript."""
        m = re.findall(r"\[tool:shell\]\n(.*?)(?=\n\n|\Z)", prompt, re.S)
        if not m:
            return ""
        lines = [l.strip() for l in m[-1].splitlines() if l.strip()]
        return " ".join(lines[-3:])[:200]

    def _first_url(self, prompt: str) -> str:
        """First SOURCE: URL from an answer_question result in the transcript."""
        m = re.search(r"^SOURCE:\s*(https?://\S+)", prompt, re.M)
        if m:
            return m.group(1)
        m = re.search(r"^\d+\.\s+.+\n\s+(https?://\S+)", prompt, re.M)
        return m.group(1) if m else ""

    def generate(self, prompt: str, max_tokens: int = 512) -> str:
        self.calls_made += 1
        # Recover the goal: the first "Goal:" line (the prompt ends with
        # "Goal: <goal>\nRespond now.", so take the first match only).
        goal = ""
        m = re.findall(r"^Goal: (.+)$", prompt, re.M)
        if m:
            goal = m[0].strip()

        turn = self._turn(prompt)

        # What has actually been read? read_file results are numbered lines, so
        # detect repeats by counting successful read_file turns in the transcript.
        read_turns = prompt.count("[tool:read_file]")
        already_listed = "[tool:list_dir]" in prompt
        already_searched = "[tool:search_files]" in prompt

        create_m = CREATE_RE.search(goal)
        read_m = READ_RE.search(goal)
        remember_m = REMEMBER_RE.search(goal)
        question_m = QUESTION_RE.search(goal)
        fix_m = FIX_RE.search(goal)
        verify_m = VERIFY_RE.search(goal)
        # "capital france" is a question even without "what is"
        if not question_m and normalize_query(goal) != goal.strip():
            question_m = True

        if self.calls_made >= self.max_calls:
            return "I have gathered enough; the task is complete."

        # A verification request: gather sources, then check the claim against
        # them and report a confidence label.
        if verify_m:
            return self._verify_turn(goal, prompt)

        # A fix request: reproduce, search the web for the error, then edit.
        if fix_m:
            return self._fix_turn(goal, prompt)

        # Not a command, not a file task, not a fact to remember, not a fix:
        # treat it as something to look up on the web. This is what makes a
        # bare word like "python" answerable.
        if not (create_m or read_m or remember_m or verify_m):
            if "[tool:answer_question]" not in turn:
                return ("I will look that up.\n"
                        + _call("answer_question", question=goal, read_page=True))
            return "I have the answer; the task is complete."

        # A question: answer it from a cited source first, and if the answer is
        # not encyclopedic, search the web.
        if question_m:
            if "[tool:answer_question]" not in turn and \
                    "[tool:web_search]" not in turn:
                return ("I will look this up and cite the source.\n"
                        + _call("answer_question", question=goal,
                                read_page=True))
            if "[tool:answer_question]" in turn and "[tool:browse_url]" not in turn:
                url = self._first_url(prompt)
                if not url:
                    return ("The web sources were inconclusive; the task is "
                            "complete.")
                return "I will read the top source page to confirm.\n" + \
                    _call("browse_url", url=url, max_chars=2000)
            return "I have the sourced answer; the task is complete."

        # "remember that X is Y" -> store key X, value Y in memory.
        if remember_m and "[tool:memory_put]" not in turn:
            fact = remember_m.group(1).strip().rstrip(".,;")
            # split on the first " is " / " are " / "=" separator, case-insensitive
            sep = re.search(r"\s+(?:is|are|was|were)\s+|[:=]\s*", fact, re.I)
            if sep:
                key = fact[:sep.start()].strip()
                value = fact[sep.end():].strip()
                # tidy a leading article so keys read naturally
                key = re.sub(r"^(?:the|a|an)\s+", "", key, flags=re.I).strip()
            else:
                key, value = fact, fact
            key = re.sub(r"\s+", "_", key)[:60] or "fact"
            return "I will store that fact.\n" + _call(
                "memory_put", key=key, value=value[:500])

        # Create a file that the goal names, if it does not exist yet.
        if create_m:
            path = create_m.group(1)
            if not os.path.exists(path):
                return ("I will create the requested file.\n"
                        + _call("write_file", path=path,
                                content=f"# {os.path.basename(path)}\n"))
            return "The file already exists; the task is complete."

        # Read a named file.
        if read_m:
            path = read_m.group(1)
            if os.path.isfile(path):
                if read_turns >= 1:
                    return "I already read that file; the task is complete."
                return "I will read that file.\n" + _call("read_file", path=path)
            if not already_searched:
                return "That file is not where I looked; I will search for it.\n" + \
                    _call("search_files", pattern=re.escape(os.path.basename(path)),
                          path=".", glob="*")
            return "I could not find the file; the task is complete."

        # Otherwise: explore, then stop.
        if not already_listed:
            return "I will look at the working directory first.\n" + \
                _call("list_dir", path=".")
        if not already_searched and "find" in goal.lower():
            return "I will search for relevant files.\n" + \
                _call("search_files", pattern=goal.split()[-1] if goal.split() else ".",
                      path=".", glob="*")
        return "I have enough context; the task is complete."
