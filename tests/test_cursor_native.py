"""Exercise metadata decoding with synthetic buffers; never connect to a display."""
import json
from pathlib import Path
import shutil
import subprocess

import pytest


def test_pipewire_cursor_replacement_and_invalidation(tmp_path):
    if not shutil.which('cc') or not shutil.which('pkg-config'):
        pytest.skip('native compiler/PipeWire headers unavailable')
    flags = subprocess.run(['pkg-config','--cflags','--libs','libpipewire-0.3'], capture_output=True,text=True)
    if flags.returncode:
        pytest.skip('PipeWire development files unavailable')
    source = Path(__file__).resolve().parents[1]/'gamestream/cursor_pipewire.c'
    harness = tmp_path/'cursor_test.c'
    harness.write_text('#define main unused_reader_main\n#include '+json.dumps(str(source))+'''\n#undef main
#include <stddef.h>
int main(void) {
    struct reader r = {.width=1920,.height=1080};
    struct {struct spa_meta_cursor c; struct spa_meta_bitmap b; unsigned char p[4];} box={0};
    struct spa_meta m={.type=SPA_META_Cursor,.size=sizeof(box),.data=&box};
    box.c.id=1; box.c.position.x=40; box.c.position.y=50;
    box.c.bitmap_offset=offsetof(__typeof__(box),b);
    box.b.format=SPA_VIDEO_FORMAT_RGBA; box.b.size=SPA_RECTANGLE(1,1);
    box.b.stride=4; box.b.offset=sizeof(box.b);
    box.p[0]=255; box.p[3]=255;
    emit_cursor(&r,&m); /* loading sprite */
    box.c.id=0; emit_cursor(&r,&m); /* hidden/invalid: must not leave loading visible */
    box.c.id=1; box.c.bitmap_offset=0; emit_cursor(&r,&m); /* valid position-only: reuse */
    box.c.bitmap_offset=offsetof(__typeof__(box),b); box.b.format=SPA_VIDEO_FORMAT_BGRx;
    box.p[0]=0; box.p[1]=255; box.p[2]=0; box.p[3]=0; emit_cursor(&r,&m);
    box.b.format=SPA_VIDEO_FORMAT_ARGB;
    box.p[0]=255; box.p[1]=0; box.p[2]=0; box.p[3]=255; emit_cursor(&r,&m);
    box.b.format=SPA_VIDEO_FORMAT_UNKNOWN; emit_cursor(&r,&m); /* replacement clears */
    box.c.bitmap_offset=0; emit_cursor(&r,&m); /* no old targeting cursor */
    box.c.bitmap_offset=offsetof(__typeof__(box),b); box.b.format=SPA_VIDEO_FORMAT_RGBA;
    box.p[0]=255; box.p[1]=255; box.p[2]=255; box.p[3]=255; emit_cursor(&r,&m);
    emit_cursor(&r,NULL); /* no current valid cursor */
    box.b.offset=0; emit_cursor(&r,&m); /* explicit empty bitmap */
    return 0;
}
''')
    executable=tmp_path/'cursor_test'
    subprocess.run(['cc','-Wall','-Wextra','-Werror',str(harness),'-o',str(executable),*flags.stdout.split()],check=True)
    rows=[json.loads(line) for line in subprocess.check_output([str(executable)],text=True).splitlines()]
    assert rows[0]['rgba_hex']=='ff0000ff'
    assert rows[1]['visible'] is False
    assert rows[2]['visible'] is True and 'rgba_hex' not in rows[2]
    assert rows[3]['rgba_hex']=='00ff00ff'
    assert rows[4]['rgba_hex']=='0000ffff'
    assert rows[5]['image'] is None and not rows[5]['visible']
    assert not rows[6]['visible']
    assert rows[7]['rgba_hex']=='ffffffff'
    assert not rows[8]['visible']
    assert rows[9]['image'] is None and not rows[9]['visible']
