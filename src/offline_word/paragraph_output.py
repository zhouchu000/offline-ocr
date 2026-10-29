"""Paragraph boundaries for flowing Word text, including recovered source rows."""
from __future__ import annotations

from dataclasses import replace
import re
import statistics

from .domain import Block, PageResult
from .line_geometry import complete_line_geometry, same_printed_line


def _editable_lines(block: Block):
    if block.edited_text is not None or not complete_line_geometry(block):
        return []
    lines = sorted(block.text_lines, key=lambda line: (line.bbox[1], line.bbox[0]))
    positions = [i for i, char in enumerate(block.original_text) if not char.isspace()]
    if "".join("".join(line.text.split()) for line in lines) != "".join(block.original_text.split()):
        return []
    result, offset = [], 0
    for line in lines:
        length = len("".join(line.text.split()))
        if not length:
            return []
        start, end = positions[offset], positions[offset+length-1]+1
        ranges = [(max(a, start)-start, min(b, end)-start) for a, b in block.bold_ranges
                  if max(a, start) < min(b, end)]
        if block.font_bold and not block.bold_ranges:
            ranges = [(0, end-start)]
        result.append((line, block.original_text[start:end], ranges, block, start, end))
        offset += length
    return result


def _heading_start(block: Block) -> bool:
    return (block.kind != "text" or block.font_bold or any(a < 4 and b-a >= 3 for a,b in block.bold_ranges)
            or bool(re.match(r"^(?:[（(]?[一二三四五六七八九十]+[、）)]|[（(]?[0-9]+[、.)）])", block.text.strip())))


def _has_stop(text: str) -> bool:
    return bool(re.search(r"[。！？!?；;：:.]\s*[’”\"」』）)]*$", text.strip()))


def _merge(first: Block, second: Block) -> Block:
    parts = sorted(_editable_lines(first)+_editable_lines(second), key=lambda part: (part[0].bbox[1],part[0].bbox[0]))
    text, ranges, lines = "", [], []
    previous_owner, previous_end = None, 0
    for line, fragment, weights, owner, start, end in parts:
        if text:
            text += owner.original_text[previous_end:start] if owner is previous_owner else "\n"
        ranges.extend((a+len(text), b+len(text)) for a,b in weights)
        text += fragment
        lines.append(line)
        previous_owner, previous_end = owner, end
    box = (min(line.bbox[0] for line in lines),min(line.bbox[1] for line in lines),
           max(line.bbox[2] for line in lines),max(line.bbox[3] for line in lines))
    return replace(first, original_text=text, bbox=box, text_lines=lines, font_bold=False, bold_ranges=ranges)


def paragraph_page(page: PageResult) -> PageResult:
    """Return an export copy with confidently placed recovery rows reattached.

    Ordinary structure blocks keep their paragraph boundaries. Recovery ids are
    assigned by the worker; they do not stand for independently detected prose
    paragraphs. Uncertain or manually edited fragments remain independent.
    """
    blocks = list(page.blocks)
    pending = {i for i,b in enumerate(blocks) if re.search(r":recovered-[0-9]+$", b.id)
               and b.kind == "text" and _editable_lines(b)}
    roots = {i for i,b in enumerate(blocks) if i not in pending and b.kind == "text" and _editable_lines(b)}
    consumed = set()
    while pending:
        choices = []
        for index in pending:
            fragment = blocks[index]
            for owner in roots:
                block = blocks[owner]
                height = statistics.median(line.bbox[3]-line.bbox[1] for line in block.text_lines+fragment.text_lines)
                if abs(block.bbox[0]-fragment.bbox[0]) > height*.8:
                    continue
                if any(same_printed_line(line.bbox, other.bbox) for line in block.text_lines for other in fragment.text_lines):
                    continue
                inside = block.bbox[1] < fragment.bbox[1] and fragment.bbox[3] < block.bbox[3]
                before = fragment.bbox[3] <= block.bbox[1]+height*.15
                after = block.bbox[3] <= fragment.bbox[1]+height*.15
                gap = block.bbox[1]-fragment.bbox[3] if before else fragment.bbox[1]-block.bbox[3]
                if not inside:
                    if not (before or after) or gap > height*.8:
                        continue
                    leading, following = (fragment,block) if before else (block,fragment)
                    if _has_stop(leading.text) or _heading_start(following):
                        continue
                    first_line = min(following.text_lines,key=lambda line: line.bbox[1])
                    if first_line.bbox[0]-min(block.bbox[0],fragment.bbox[0]) > height*.8:
                        continue
                # Never bridge an intervening heading, table or illustration.
                low, high = sorted(((block.bbox[1]+block.bbox[3])/2,(fragment.bbox[1]+fragment.bbox[3])/2))
                if any(i not in {index,owner} and i not in consumed and i not in pending
                       and low < (other.bbox[1]+other.bbox[3])/2 < high
                       and min(other.bbox[2],block.bbox[2]) > max(other.bbox[0],block.bbox[0])
                       for i,other in enumerate(blocks)):
                    continue
                choices.append((-1 if inside else max(0,gap),index,owner))
        if not choices:
            break
        # Equal support from two neighboring paragraphs is not permission to
        # pick one arbitrarily. Keep that recovered row independently editable.
        choices = [choice for choice in choices if not any(
            other[1] == choice[1] and other[2] != choice[2]
            and (other[0] < 0) == (choice[0] < 0)
            and abs(other[0]-choice[0]) <= (blocks[choice[1]].bbox[3]-blocks[choice[1]].bbox[1])*.15
            for other in choices)]
        if not choices:
            break
        _, index, owner = min(choices)
        blocks[owner] = _merge(blocks[owner],blocks[index])
        pending.remove(index)
        consumed.add(index)
    return replace(page,blocks=[block for i,block in enumerate(blocks) if i not in consumed])


def continues_on_next_page(previous: Block, following: Block) -> bool:
    """Join only unindented, full-row continuations across scanned sheets."""
    if (previous.kind != "text" or following.kind != "text" or previous.edited_text is not None
            or following.edited_text is not None or len(previous.text.strip()) < 8
            or len(following.text.strip()) < 8 or _has_stop(previous.text) or _heading_start(following)
            or not _editable_lines(previous) or not _editable_lines(following)):
        return False
    last = max(previous.text_lines,key=lambda line: line.bbox[1])
    first = min(following.text_lines,key=lambda line: line.bbox[1])
    height = first.bbox[3]-first.bbox[1]
    prior_height = last.bbox[3]-last.bbox[1]
    return (first.bbox[0]-following.bbox[0] < height*.7
            and previous.bbox[2]-last.bbox[2] < prior_height*.8
            and not previous.font_bold and not following.font_bold)
