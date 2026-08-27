"""
Surface adapters.

The seam: the artifact says *what control, semantically*; the adapter knows how
to find and touch it on this surface. Everything Playwright-specific lives here
and nowhere else, which is what makes a desktop adapter a new file rather than a
schema change.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Protocol

from playwright.sync_api import Frame, Locator, Page

from cua.schema.artifact import ControlRef, LocatorStrategy


@dataclass
class Observation:
    """What the agent sees. Deliberately not a DOM.

    `tree` is a flattened accessibility rendering, one block per frame. This is
    the same text replay hashes for no-progress detection and the same text the
    LLM reasons over during discovery — so the model can never rely on
    information replay will not have.
    """

    url: str
    tree: str
    screenshot_png: bytes | None = None
    frames: list[str] = field(default_factory=list)

    def digest(self) -> str:
        return hashlib.sha256(self.tree.encode()).hexdigest()[:16]


class Resolution:
    """Outcome of trying to find a control."""

    def __init__(self, locator: Locator | None, strategy: str | None,
                 depth: int, error: str | None = None):
        self.locator = locator
        self.strategy = strategy
        self.depth = depth
        self.error = error

    @property
    def ok(self) -> bool:
        return self.locator is not None


class SurfaceAdapter(Protocol):
    """The interface a desktop (UIA / AT-SPI) adapter would also implement."""

    def snapshot(self, with_screenshot: bool = False) -> Observation: ...
    def resolve(self, ref: ControlRef) -> Resolution: ...
    def navigate(self, url: str) -> None: ...
    def click(self, res: Resolution) -> None: ...
    def type_text(self, res: Resolution, text: str, clear: bool) -> None: ...
    def select(self, res: Resolution, value: str) -> None: ...
    def read(self, res: Resolution) -> str: ...
    def current_url(self) -> str: ...
    def contains_text(self, text: str, case_sensitive: bool = False) -> bool: ...


# ---------------------------------------------------------------------------


def candidate_strategies(ref: ControlRef) -> list[tuple[LocatorStrategy, str]]:
    """Ordered candidate list for a ControlRef.

    role+name is the implicit first candidate *when a name exists*. When it does
    not — common in legacy markup where inputs sit in bare table cells — the
    first declared fallback becomes depth 0, so `fallback_depth` stays an honest
    drift signal instead of being permanently offset for those controls.
    """
    out: list[tuple[LocatorStrategy, str]] = []
    if ref.name:
        out.append((LocatorStrategy.ROLE_NAME, ref.name))
    out.extend((h.strategy, h.value) for h in ref.fallbacks)
    if not out:
        out.append((LocatorStrategy.ROLE_NAME, ""))
    return out


class WebSurfaceAdapter:
    """Covers both `web` and `legacy_web`.

    Legacy support is not a separate class because the only real difference is
    which strategies carry the weight: framesets are handled by FrameRef.path,
    and table-based layout by the TABLE_CELL / TEXT_ANCHOR strategies.
    """

    def __init__(self, page: Page):
        self.page = page

    # --- perception -------------------------------------------------------

    def _frames(self) -> list[Frame]:
        return list(self.page.frames)

    def snapshot(self, with_screenshot: bool = False) -> Observation:
        blocks, names = [], []
        for fr in self._frames():
            label = fr.name or ("(main)" if fr.parent_frame is None else "(anonymous)")
            names.append(label)
            # A frameset document has no <body>; its children are the frames we
            # already enumerate separately, so it contributes nothing.
            try:
                if fr.locator("frameset").count() > 0:
                    aria = "(frameset container)"
                else:
                    aria = fr.locator("body").aria_snapshot(timeout=2000)
            except Exception as exc:  # frame detached mid-snapshot
                aria = f"<unavailable: {type(exc).__name__}>"
            blocks.append(f"### frame: {label}  url={fr.url}\n{aria}")

        shot = None
        if with_screenshot:
            try:
                shot = self.page.screenshot(full_page=False)
            except Exception:
                shot = None

        return Observation(url=self.page.url, tree="\n\n".join(blocks),
                           screenshot_png=shot, frames=names)

    # --- resolution -------------------------------------------------------

    def _scope(self, ref: ControlRef) -> Frame:
        """Walk FrameRef.path by frame *name*, never by index.

        Frame ordering is the first thing that shifts between tenant builds of
        the same vendor product; names are configuration, not layout.
        """
        if not ref.frame or not ref.frame.path:
            return self.page.main_frame
        current = self.page.main_frame
        for want in ref.frame.path:
            match = next((f for f in self.page.frames if f.name == want), None)
            if match is None:
                raise LookupError(f"frame '{want}' not found")
            current = match
        return current

    def resolve(self, ref: ControlRef) -> Resolution:
        try:
            frame = self._scope(ref)
        except LookupError as exc:
            return Resolution(None, None, -1, str(exc))

        errors = []
        for depth, (strategy, value) in enumerate(candidate_strategies(ref)):
            try:
                loc = self._apply(frame, ref, strategy, value)
                if loc is None:
                    continue
                count = loc.count()
                if count == 1:
                    return Resolution(loc, strategy.value, depth)
                if count > 1 and ref.nth is not None:
                    return Resolution(loc.nth(ref.nth), strategy.value, depth)
                errors.append(f"{strategy.value}: matched {count}")
            except Exception as exc:
                errors.append(f"{strategy.value}: {type(exc).__name__}")

        return Resolution(None, None, -1,
                          f"no strategy resolved to exactly one control ({'; '.join(errors)})")

    def _apply(self, frame: Frame, ref: ControlRef,
               strategy: LocatorStrategy, value: str) -> Locator | None:
        if strategy == LocatorStrategy.ROLE_NAME:
            kwargs = {}
            if value:
                kwargs["name"] = re.compile(value) if ref.name_match == "regex" else value
                kwargs["exact"] = ref.name_match == "exact"
            loc = frame.get_by_role(ref.role, **kwargs)  # type: ignore[arg-type]
            return self._narrow(frame, loc, ref)

        if strategy == LocatorStrategy.LABEL_PROXIMITY:
            return frame.get_by_label(value)

        if strategy == LocatorStrategy.TABLE_CELL:
            return self._table_cell(frame, value)

        if strategy == LocatorStrategy.TEXT_ANCHOR:
            return self._text_anchor(frame, ref, value)

        if strategy == LocatorStrategy.CSS:
            return frame.locator(value)

        if strategy == LocatorStrategy.XPATH:
            return frame.locator(f"xpath={value}")

        if strategy == LocatorStrategy.RELATIVE_COORDS:
            return None  # handled by act(), not resolvable to a Locator

        return None

    def _narrow(self, frame: Frame, loc: Locator, ref: ControlRef) -> Locator:
        """Apply semantic anchors that survive re-branding."""
        if ref.within_section:
            section = frame.locator(
                f"xpath=//*[contains(., {_xq(ref.within_section)})][not(.//*[contains(., {_xq(ref.within_section)})])]/ancestor::table[1]"
            )
            if section.count() >= 1:
                scoped = section.first.locator(loc)
                if scoped.count() >= 1:
                    return scoped
        return loc

    def _table_cell(self, frame: Frame, spec: str) -> Locator | None:
        """spec: 'row=<row label>;col=<column header>'

        The portable way to address data in table-laid-out legacy screens: row
        and column *labels* are vendor-fixed strings, whereas cell position and
        markup are exactly what tenant branding changes.
        """
        parts = dict(p.split("=", 1) for p in spec.split(";") if "=" in p)
        row_label, col_label = parts.get("row"), parts.get("col")
        if not row_label or not col_label:
            return None

        header_row = frame.locator(
            f"xpath=//tr[td[normalize-space(.)={_xq(col_label)}]]"
        )
        if header_row.count() == 0:
            return None
        headers = header_row.first.locator("td")
        col_index = None
        for i in range(headers.count()):
            if (headers.nth(i).inner_text() or "").strip() == col_label:
                col_index = i
                break
        if col_index is None:
            return None

        target_row = frame.locator(
            f"xpath=//tr[td[normalize-space(.)={_xq(row_label)}]]"
        )
        if target_row.count() == 0:
            return None
        return target_row.first.locator("td").nth(col_index)

    def _text_anchor(self, frame: Frame, ref: ControlRef, spec: str) -> Locator | None:
        """spec: 'after=<text>' — the n-th control of ref.role following some text.

        The xpath supplies *position only*; `ref.role` still decides what kind of
        control we are looking for. Keeping those separate matters: a legacy
        label cell is typically followed by both its input and the form's submit
        button, so position alone is ambiguous by construction. It also keeps the
        strategy honest about the portability claim — role is the semantic part
        and survives a port to UIA/AX, the xpath is only "which side of the label".
        """
        parts = dict(p.split("=", 1) for p in spec.split(";") if "=" in p)
        anchor = parts.get("after")
        if not anchor:
            return None
        positional = frame.locator(
            f"xpath=//*[contains(normalize-space(.), {_xq(anchor)})]"
            f"/following::*[self::input or self::a or self::button"
            f" or self::select or self::textarea]"
        )
        matching = frame.get_by_role(ref.role, include_hidden=False).and_(positional)  # type: ignore[arg-type]

        # Nearest-following, not all-following. Elsewhere a strategy matching
        # more than one control is a failure, because two controls answering to
        # the same *identity* means we cannot tell which was recorded. This
        # strategy addresses by *position* instead — "the control after this
        # label" — and every later control on the form also follows that label,
        # so a multi-match is expected rather than ambiguous. On the sub-account
        # form the "Fund From" select follows the "Account Type" label as surely
        # as its own does. Document order makes the nearest one first.
        return matching.nth(ref.nth if ref.nth is not None else 0)

    # --- action -----------------------------------------------------------

    def navigate(self, url: str) -> None:
        self.page.goto(url, wait_until="load")

    def click(self, res: Resolution) -> None:
        assert res.locator is not None
        res.locator.click(timeout=10_000)

    def type_text(self, res: Resolution, text: str, clear: bool) -> None:
        assert res.locator is not None
        if clear:
            res.locator.fill("")
        res.locator.fill(text)

    def select(self, res: Resolution, value: str) -> None:
        assert res.locator is not None
        res.locator.select_option(value)

    def read(self, res: Resolution) -> str:
        assert res.locator is not None
        return (res.locator.inner_text() or "").strip()

    def current_url(self) -> str:
        return self.page.url

    def contains_text(self, text: str, case_sensitive: bool = False) -> bool:
        """Searched across every frame — a frameset app puts errors in one pane."""
        needle = text if case_sensitive else text.lower()
        for fr in self._frames():
            try:
                # A frameset document has no <body>; its text lives in the child
                # frames, which this same loop visits. Asking for one anyway costs
                # the full locator timeout on every call — and this runs once per
                # global condition per step, so it dominated replay wall-clock
                # (~2s x 6 conditions x 4 steps) before the guard.
                if fr.locator("frameset").count() > 0:
                    continue
                body = fr.locator("body").inner_text(timeout=1000) or ""
            except Exception:
                continue
            hay = body if case_sensitive else body.lower()
            if needle in hay:
                return True
        return False


def _xq(s: str) -> str:
    """Quote a string for XPath, handling embedded quotes."""
    if '"' not in s:
        return f'"{s}"'
    if "'" not in s:
        return f"'{s}'"
    parts = s.split('"')
    return "concat(" + ', \'"\', '.join(f'"{p}"' for p in parts) + ")"
