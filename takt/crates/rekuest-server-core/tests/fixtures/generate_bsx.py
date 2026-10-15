"""What arkitekt-spec's parser makes of BSX sources: the corpus `tests/bsx.rs` holds takt to.

Every case is a source and either the tree the spec's `bsx()` parses it into, or the error it
raises. takt's parser must give the same tree for every valid source and refuse every invalid one.

Run with arkitekt-spec importable, from the repository root:
    uv run --no-project --with ~/Code/packages/arkitekt-spec \
        python takt/crates/rekuest-server-core/tests/fixtures/generate_bsx.py
The Orkestrator fixtures are added, unparsed back into BSX, when a checkout is found at
$ORKESTRATOR (default ~/Code/standalones/orkestrator-next).
"""

import json
import os
from pathlib import Path

from arkitekt_spec.actions import ComponentNodeInput
from arkitekt_spec.declare.blok.parser import bsx
from arkitekt_spec.declare.blok.unparse import to_bsx

HERE = Path(__file__).parent

CROP_LOGO = """
<SVG viewBox="0 0 64 64" title="Crop">
  <Defs>
    <LinearGradient id="ground" x1="0" y1="0" x2="1" y2="1">
      <Stop offset="0" stopColor="#0b1220" />
      <Stop offset="1" stopColor="#1b2a44" />
    </LinearGradient>
    <ClipPath id="tile"><Rect x="0" y="0" width="64" height="64" rx="14" /></ClipPath>
  </Defs>
  <G clipPath="url(#tile)">
    <Rect x="0" y="0" width="64" height="64" fill="url(#ground)" />
    <Ellipse cx="13" cy="14" rx="7" ry="5.5" fill="url(#cell)" transform="rotate(-20 13 14)" />
    <Path d="M0 0H64V64H0Z M19 19V45H45V19Z" fillRule="evenodd" fill="#020617" fillOpacity="0.62" />
  </G>
  <Path d="M19 9V45H55" fill="none" stroke="#fbbf24" strokeWidth="4.2" strokeLinecap="round" />
</SVG>
"""

VALID = [
    "<Page />",
    '<Card id="ot2-monitor"><Button /></Card>',
    "<Card>\n  <Button />\n  <Button />\n  <Text text='a' />\n</Card>",
    '<Page color="#fff"><Text text="@fff" /></Page>',
    '<StructurePicker identifier="@mikro/arraydataset" />',
    '<foreach items="@dep.state.xs" let="#item"><Text text="@item.name" /></foreach>',
    '<ForEach items="@dep.xs" let="#row" />',
    '<Text value="@utils.math.multiply(-2, dep.state.x)" />',
    '<Text v="@utils.gt(dep.state.x, 2)" />',
    '<Text v="@utils.gt(a=dep.state.x, b=2.5)" />',
    '<Button onClick="@self.run(iterations=5).into(job)" />',
    '<Button onClick="@self.into(job)" />',
    '<Text value="$state.scope.Stage.x" />',
    '<Text value="@scope.Stage.x" />',
    '<Robot onJointChange="@arm.move_joint(name=event.name, value=event.value)" />',
    '<Slider onChange="@arm.set(event)" />',
    '<Button onClick="@actions.stage.move(1, 2)" />',
    '<Button onClick="@self.go([1, 2.5, -3, +4], {\'a\': 1, &quot;b&quot;: [True, False]}, (1,), ())" />',
    '<Button onClick="@self.go(0x1F, 0o17, 0b101, 1_000, 1e3, .5, 2.)" />',
    '<Button onClick="@self.say(\'it\\\'s\', &quot;tab\\there&quot;, \'\\x41\\u00e9\', r\'raw\\n\')" />',
    '<Button onClick="@self.say(\'a\' \'b\', u\'c\')" />',
    '<Button onClick="@self.nest(utils.add(1, 2), other.op(x=self.a.b))" />',
    '<Button onClick="@self.go(1, 2,)" />',
    '<Button onClick="@self.go(b\'x\', Rb\'\\d\')" />',
    '<Text text="  padded  " value="@dep.x " />',
    '<Text text="&lt;b&gt; &amp; &#x41;" />',
    "<Text text='a &quot;quote&quot;' />",
    "<Card><!-- a comment --><Button /></Card>",
    '<Card>\n  <Button\n    label="multi\n line"\n  />\n</Card>',
    '<Text text="plain text with spaces" color="#abc" />',
    '<Text text="@(dep.x)" />',
    CROP_LOGO,
]

INVALID = [
    "<Page>\n  <Header />\n  <Label>\n</Page>\n<Footer />",
    "<Page>\n  <Label>\n        ",
    '<Badge className="x">Opentrons is getting hot!</Badge>',
    "<Card><Button />trailing</Card>",
    '<Text text="@a / b" />',
    '<foreach items="@dep.state.xs" let="item" />',
    '<foreach items="@dep.state.xs" let="#1x" />',
    '<Button onClick="@self.run().into()" />',
    '<Button onClick="@self.run().into(a, b)" />',
    '<Button onClick="@self.run().into(form.job)" />',
    '<Button onClick="@self.run().into(name=job)" />',
    '<Button onClick="@utils.gt(1, 2).into(job)" />',
    '<Text value="$state.a" />',
    '<Text value="$dep.a.b" />',
    '<Button onClick="@self.go(None)" />',
    '<Button onClick="@self.go(f\'x\')" />',
    '<Button onClick="@self.go(**kw)" />',
    '<Button onClick="@self.go(*xs)" />',
    '<Button onClick="@self.go({1: 2})" />',
    '<Button onClick="@self.go({\'\': 2})" />',
    '<Button onClick="@self.go(-x)" />',
    '<Button onClick="@self.go(a[0])" />',
    '<Button onClick="@self.go(1 + 2)" />',
    '<Button onClick="@go()" />',
    '<Button onClick="@utils()" />',
    '<Button onClick="@actions.dep()" />',
    '<Button onClick="@self.a().b()" />',
    '<Button onClick="@1" />',
    '<Button onClick="@self.go(a=1, 2)" />',
    '<Button onClick="@self.go(\'unterminated)" />',
    '<Button onClick="@utils.gt(1, 2, 3)" />',
    '<Button onClick="@utils.gt(1, c=2)" />',
    '<Button onClick="@utils.gt(1, a=2)" />',
    '<Button onClick="@utils.gt(1)" />',
    '<Button onClick="@class.go()" />',
    "",
    "<a/><b/>",
    '<Text value="@ dep.x" />',
]


def orkestrator_sources() -> list[str]:
    root = Path(os.environ.get("ORKESTRATOR", Path.home() / "Code/standalones/orkestrator-next"))
    bloks = root / "src/renderer/src/core/blok/renderer/bloks"
    sources = []
    for fixture in (bloks / "scene/scene.fixture.json", bloks / "logo/logo.fixture.json"):
        if fixture.is_file():
            for tree in json.loads(fixture.read_text()):
                sources.append(to_bsx(ComponentNodeInput.model_validate(tree)))
    return sources


def main() -> None:
    cases = []
    for source in [*VALID, *orkestrator_sources()]:
        tree = bsx(source)
        cases.append({"source": source, "tree": tree.model_dump(mode="json", exclude_none=True)})
    for source in INVALID:
        try:
            bsx(source)
        except ValueError as error:
            cases.append({"source": source, "error": str(error)})
        else:
            raise SystemExit(f"expected a refusal, the spec parsed: {source!r}")
    (HERE / "bsx.json").write_text(json.dumps(cases, indent=1, ensure_ascii=False) + "\n")
    print(f"{len(cases)} cases: {sum('tree' in c for c in cases)} valid, {sum('error' in c for c in cases)} refused")


if __name__ == "__main__":
    main()
