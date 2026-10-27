"""The BSX corpus `tests/bsx.rs` holds takt to: `bsx_v2.json`, written by arkitekt-spec itself
(`arkitekt_spec.blok.corpus`): parsed cases with their tree, refused ones with their diagnostic
code (and line, column), checked ones with the codes the validator finds against a scope. It
carries the vocab version, the catalogs and the text table it was parsed with. Parity compares
trees and codes, never messages. takt speaks blok language v2 only.

Run with arkitekt-spec importable, from the repository root:
    uv run --no-project --with ~/Code/packages/arkitekt-spec \\
        python takt/crates/rekuest-server-core/tests/fixtures/generate_bsx.py
"""

from pathlib import Path

from arkitekt_spec.blok import corpus

HERE = Path(__file__).parent

if __name__ == "__main__":
    print(corpus.write(HERE))
