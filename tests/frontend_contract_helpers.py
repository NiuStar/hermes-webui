"""Small source-fixture helpers; no arbitrary lookahead windows."""
import json
import re
import shutil
import subprocess

import pytest


def max_width_media_blocks(css, width):
    """Read every matching media block, not only the first occurrence."""
    css = re.sub(r'/\*.*?\*/', '', css, flags=re.S)
    blocks = []
    for match in re.finditer(rf'@media\s*\(\s*max-width\s*:\s*{width}px\s*\)\s*\{{', css):
        opening = match.end() - 1
        depth = 0
        for index in range(opening, len(css)):
            if css[index] == '{':
                depth += 1
            elif css[index] == '}':
                depth -= 1
                if depth == 0:
                    blocks.append(css[opening + 1:index])
                    break
        else:
            raise AssertionError('unterminated media block')
    assert blocks, f'missing max-width:{width}px media blocks'
    return '\n'.join(blocks)


def run_node(script):
    node = shutil.which('node')
    if not node:
        pytest.skip('node unavailable')
    result = subprocess.run([node], input=script, text=True, capture_output=True, timeout=30)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def function_source(source, name):
    """Extract repository functions using the existing balanced-brace fixture."""
    start = re.search(rf'(?:async\s+)?function\s+{re.escape(name)}\(', source)
    assert start, f'missing function {name}'
    opening = source.index('{', start.start())
    depth = 0
    for index in range(opening, len(source)):
        if source[index] == '{':
            depth += 1
        elif source[index] == '}':
            depth -= 1
            if depth == 0:
                return source[start.start():index + 1]
    raise AssertionError(f'unterminated function {name}')
