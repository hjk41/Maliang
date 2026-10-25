"""Audit every bundled glyph and its Qt metric proxy, without hardware."""
import argparse
from collections import Counter
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import zlib
from contextlib import closing

from fontTools.ttLib import TTFont

ROOT=Path(__file__).resolve().parents[1]/'pagewriter/resources/fonts'
IDS={'maoken','wenkai-gb','xiaolai','yozai','hanzi-writer',
     'ems-readability','ems-tech','ems-neato','ems-allure'}


def audit(root=ROOT):
    catalog=json.loads((root/'catalog.json').read_text(encoding='utf-8'))
    assert {f['id'] for f in catalog['fonts']}==IDS
    reports=[]
    for font in catalog['fonts']:
        for field,hash_field in [('file','sha256'),('layout_file','layout_sha256')]:
            path=root/font[field]
            assert hashlib.sha256(path.read_bytes()).hexdigest()==font[hash_field],path
        notice=(root/font['license_file']).read_text(encoding='utf-8')
        assert font['source_name'] in notice and font['source_sha256'] in notice
        assert ('SIL OPEN FONT LICENSE' in notice if font['license']=='OFL-1.1' else '文鼎公眾授權書' in notice)
        if font['id']=='hanzi-writer':
            source=json.loads((Path(__file__).resolve().parent/'font_sources.json').read_text(encoding='utf-8'))
            original=next(f for f in source['fonts'] if f['id']=='hanzi-writer')
            assert hashlib.sha256((root/'ARPHICPL.TXT').read_bytes()).hexdigest()==original['license_sha256']
        layout=TTFont(root/font['layout_file'])
        cmap=layout.getBestCmap()
        assert layout['head'].unitsPerEm==1000
        assert layout['name'].getDebugName(1)==font['family']
        assert all(layout['glyf'][name].numberOfContours==0 for name in layout.getGlyphOrder()), 'Metric proxy contains drawn outlines'
        if font['id']=='yozai':
            assert 'yoz' not in font['family'].lower() and 'y.oz' not in font['family'].lower()
        if font['id']=='wenkai-gb':
            assert not any(name in font['family'] for name in ['霞鹜','霞鶩','落霞孤鹜','落霞孤鶩','LXGW'])
        scalars=set()
        totals=Counter()
        with closing(sqlite3.connect((root/font['file']).as_uri()+'?mode=ro',uri=True)) as db:
            assert db.execute('PRAGMA quick_check').fetchone()[0]=='ok'
            metadata={key:json.loads(value) for key,value in db.execute('SELECT key,value FROM metadata')}
            for cp,advance,blob in db.execute('SELECT cp,advance,data FROM glyphs ORDER BY cp'):
                assert 0<=cp<=0x10ffff and not 0xd800<=cp<=0xdfff
                assert math.isfinite(advance) and 0<=advance<=65535
                commands=json.loads(zlib.decompress(blob))
                assert len(commands)<=200_000
                assert not commands or commands[0][0]==0
                previous=None;ink=False
                for command in commands:
                    kind,x,y=command
                    assert kind in (0,1) and type(kind) is int
                    assert all(math.isfinite(n) and abs(n)<=1_000_000 for n in (x,y))
                    ink=ink or (kind==1 and previous is not None and previous!=(x,y))
                    previous=(x,y)
                    totals['paths']+=kind==0
                assert ink or chr(cp).isspace(),f'{font["id"]}: invisible U+{cp:04X}'
                assert cp in cmap
                actual=layout['hmtx'][cmap[cp]][0]
                assert abs(actual-advance)<=.501
                scalars.add(cp)
                totals['points']+=len(commands)
        assert scalars==set(cmap)
        assert len(scalars)==font['glyph_count']
        coverage={cp for a,b in font['coverage'] for cp in range(a,b+1)}
        assert coverage==scalars
        assert metadata['source_sha256']==font['source_sha256']
        reports.append({'id':font['id'],'glyphs':len(scalars),'bytes':(root/font['file']).stat().st_size,
                        'paths':totals['paths'],'points':totals['points'],'all_glyphs_verified':True})
        print(json.dumps(reports[-1]),flush=True)
    return reports


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path)
    args=parser.parse_args()
    reports=audit()
    if args.output:
        args.output.parent.mkdir(parents=True,exist_ok=True)
        args.output.write_text(json.dumps({'fonts':reports,'total_glyphs':sum(f['glyphs'] for f in reports)},indent=2),encoding='utf-8')
    print('All nine font databases, proxy metrics, coverage and license notices verified.')
