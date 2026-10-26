"""Build all approved offline stroke fonts, with resumable TTF conversion.

python tools/build_builtin_fonts.py --workers 6
Build-only dependencies: Pillow, fontTools, numpy, OpenCV, scikit-image.
Original sources are pinned and cached outside the publication set.
"""
import argparse
from concurrent.futures import ProcessPoolExecutor
import hashlib
import io
import json
import math
import os
from pathlib import Path
import shutil
import sqlite3
import tarfile
import time
import urllib.request
import xml.etree.ElementTree as ET
import zlib

from fontTools.fontBuilder import FontBuilder
from fontTools.pens.ttGlyphPen import TTGlyphPen
from fontTools.pens.basePen import BasePen
from fontTools.svgLib.path import parse_path
from fontTools.ttLib import TTFont
from PIL import Image, ImageDraw, ImageFont
import numpy as np

from font_conversion import centerlines

PROJECT = Path(__file__).resolve().parents[1]
OUTPUT = PROJECT/'pagewriter/resources/fonts'
CACHE = PROJECT/'.font-build'
SOURCE = json.loads((PROJECT/'tools/font_sources.json').read_text(encoding='utf-8'))
SIZE = 512
PAD = 16
_font = None


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def download(url, path, expected=None):
    if not path.is_file():
        path.parent.mkdir(parents=True,exist_ok=True)
        proxy = os.environ.get('HTTPS_PROXY') or os.environ.get('https_proxy')
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({'http':proxy,'https':proxy})) if proxy else urllib.request.build_opener()
        request = urllib.request.Request(url,headers={'User-Agent':'Maliang-font-build/1.0'})
        with opener.open(request,timeout=90) as response:
            data = response.read(100_000_000)
        temporary = path.with_suffix(path.suffix+'.tmp')
        temporary.write_bytes(data)
        temporary.replace(path)
    if expected and digest(path)!=expected:
        raise ValueError(f'Source SHA256 mismatch: {path.name}')
    return path


def source_file(item):
    extension = {'ttf':'.ttf','svg':'.svg','hanzi':'.tar.gz'}[item['kind']]
    path = CACHE/'sources'/(item['id']+extension)
    return download(item['source_url'],path,item.get('sha256'))


def connect(path):
    db = sqlite3.connect(path)
    db.execute('CREATE TABLE IF NOT EXISTS glyphs(cp INTEGER PRIMARY KEY, advance REAL NOT NULL, data BLOB NOT NULL)')
    db.execute('CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL)')
    return db


def store(db,cp,advance,commands):
    if not 0<=cp<=0x10ffff or 0xd800<=cp<=0xdfff:
        raise ValueError('Invalid Unicode scalar')
    if not math.isfinite(advance) or advance<0:
        raise ValueError('Invalid advance')
    if any(len(v)!=3 or v[0] not in (0,1) or not all(math.isfinite(x) for x in v[1:]) for v in commands):
        raise ValueError(f'Invalid commands U+{cp:04X}')
    blob = zlib.compress(json.dumps(commands,separators=(',',':')).encode(),9)
    db.execute('INSERT OR REPLACE INTO glyphs VALUES(?,?,?)',(cp,float(advance),blob))


def init_worker(path):
    global _font
    import cv2
    cv2.setNumThreads(1)
    _font=ImageFont.truetype(path,SIZE,layout_engine=ImageFont.Layout.BASIC)


def convert_character(cp):
    char=chr(cp)
    left,top,right,bottom = _font.getbbox(char,anchor='ls')
    gray=Image.new('L',(max(1,right-left)+2*PAD,max(1,bottom-top)+2*PAD),255)
    ImageDraw.Draw(gray).text((PAD-left,PAD-top),char,font=_font,fill=0,anchor='ls')
    commands=centerlines(gray,(left-PAD,top-PAD),SIZE)
    return cp,commands


def build_ttf(item,path,workers,limit=None):
    font=TTFont(path)
    cmap={cp:name for cp,name in font.getBestCmap().items()
          if cp<=0x10ffff and not 0xd800<=cp<=0xdfff and name!='.notdef'}
    upem=font['head'].unitsPerEm
    metrics={cp:font['hmtx'][name][0]*1000/upem for cp,name in cmap.items()}
    ascent=math.ceil(font['hhea'].ascent*1000/upem)
    descent=math.floor(font['hhea'].descent*1000/upem)
    work=CACHE/(item['id']+'.sqlite')
    db=connect(work)
    identity=json.dumps({'source_sha256':digest(path),'size':SIZE,'epsilon':.85,'method':'skimage-zhang-edge-trails-v2'},sort_keys=True)
    old=db.execute("SELECT value FROM metadata WHERE key='build_identity'").fetchone()
    if old and old[0]!=identity:
        db.close()
        raise ValueError('Conversion settings changed; use a fresh --cache directory')
    db.execute('INSERT OR REPLACE INTO metadata VALUES(?,?)',('build_identity',identity))
    done={cp for cp, in db.execute('SELECT cp FROM glyphs')}
    pending=sorted(set(cmap)-done)
    if limit:
        pending=pending[:limit]
    completed=len(done)
    started=time.monotonic()
    if pending:
        with ProcessPoolExecutor(max_workers=workers,initializer=init_worker,initargs=(str(path),)) as pool:
            for cp,commands in pool.map(convert_character,pending,chunksize=24):
                store(db,cp,metrics[cp],commands)
                completed+=1
                if completed%250==0:
                    db.commit()
                    print(f'{item["id"]}: {completed}/{len(cmap)} ({time.monotonic()-started:.1f}s this run)',flush=True)
    db.commit()
    actual=db.execute('SELECT COUNT(*) FROM glyphs').fetchone()[0]
    if actual!=len(cmap):
        db.close()
        raise RuntimeError(f'Incomplete conversion: {actual}/{len(cmap)}; rerun to resume')
    db.close()
    return work,ascent,descent


class StrokePen(BasePen):
    def __init__(self):
        super().__init__(None)
        self.paths=[]
        self.current=[]
    def _moveTo(self,p):
        if self.current:self.paths.append(self.current)
        self.current=[p]
    def _lineTo(self,p):self.current.append(p)
    def _curveToOne(self,a,b,c):
        p=self.current[-1]
        count=max(8,min(100,math.ceil((math.dist(p,a)+math.dist(a,b)+math.dist(b,c))/4)))
        for i in range(1,count+1):
            t=i/count;s=1-t
            self.current.append(tuple(s*s*s*p[k]+3*s*s*t*a[k]+3*s*t*t*b[k]+t*t*t*c[k] for k in (0,1)))
    def _qCurveToOne(self,a,b):
        p=self.current[-1]
        for i in range(1,21):
            t=i/20;s=1-t
            self.current.append(tuple(s*s*p[k]+2*s*t*a[k]+t*t*b[k] for k in (0,1)))
    def _closePath(self):
        if self.current:
            if self.current[-1]!=self.current[0]:self.current.append(self.current[0])
            self.paths.append(self.current);self.current=[]
    def _endPath(self):
        if self.current:self.paths.append(self.current);self.current=[]


def build_svg(item,path):
    tree=ET.parse(path)
    font=tree.find('.//{*}font')
    glyphs=[g for g in font.findall('{*}glyph') if len(g.get('unicode',''))==1]
    h=next(g for g in glyphs if g.get('unicode')=='a')
    pen=StrokePen();parse_path(h.get('d',''),pen);pen._endPath()
    ys=[p[1] for line in pen.paths for p in line]
    scale=500/(max(ys)-min(ys))
    item['normalization']={'method':'lowercase-a-height','body_height':500,'scale':scale}
    work=CACHE/(item['id']+'.sqlite')
    db=connect(work)
    ymin,ymax=0.,0.
    for glyph in glyphs:
        cp=ord(glyph.get('unicode'))
        pen=StrokePen();parse_path(glyph.get('d',''),pen);pen._endPath()
        commands=[]
        for line in pen.paths:
            if len(line)==1:line=[line[0],(line[0][0]+.01,line[0][1])]
            for i,(x,y) in enumerate(line):
                y=-y*scale
                commands.append([int(i>0),round(x*scale,3),round(y,3)])
                ymin=min(ymin,y);ymax=max(ymax,y)
        advance=float(glyph.get('horiz-adv-x',font.get('horiz-adv-x')))*scale
        store(db,cp,advance,commands)
    metadata=tree.find('{*}metadata')
    credits=''.join(metadata.itertext()) if metadata is not None else ''
    db.commit();db.close()
    return work,math.ceil(-ymin+30),-math.ceil(ymax+30),credits


def build_hanzi(item,path):
    work=CACHE/(item['id']+'.sqlite')
    db=connect(work)
    count=0
    with tarfile.open(path,'r:gz') as archive:
        for member in archive:
            parts=member.name.split('/')
            if len(parts)!=3 or parts[1]!='data' or not parts[-1].endswith('.json') or not member.isfile():continue
            name=parts[-1][:-5]
            if len(name)!=1 or member.size>1_000_000:continue
            data=json.load(archive.extractfile(member))
            commands=[]
            for line in data['medians']:
                if len(line)==1:line=[line[0],[line[0][0]+.1,line[0][1]]]
                for i,(x,y) in enumerate(line):
                    commands.append([int(i>0),round(x*1000/1024,3),round(-y*1000/1024,3)])
            store(db,ord(name),1000.,commands)
            count+=1
    db.commit();db.close()
    if count<9000:raise ValueError(f'Hanzi source unexpectedly small: {count}')
    return work,1000,-250


def ranges(values):
    result=[]
    for cp in sorted(values):
        if result and cp==result[-1][1]+1:result[-1][1]=cp
        else:result.append([cp,cp])
    return result


def layout_font(item,rows,ascent,descent,license_text):
    family=item.get('family','Maliang Stroke '+item['id'])
    builder=FontBuilder(1000,isTTF=True)
    order=['.notdef']+[f'u{cp:06X}' for cp,_ in rows]
    builder.setupGlyphOrder(order)
    builder.setupCharacterMap({cp:f'u{cp:06X}' for cp,_ in rows})
    empty=TTGlyphPen(None).glyph()
    builder.setupGlyf({name:empty for name in order})
    builder.setupHorizontalMetrics({'.notdef':(600,0),**{f'u{cp:06X}':(max(0,round(advance)),0) for cp,advance in rows}})
    builder.setupHorizontalHeader(ascent=ascent,descent=descent,lineGap=0)
    builder.setupNameTable({'familyName':family,'styleName':'Regular','uniqueFontIdentifier':family+' v1',
                           'fullName':family,'psName':family.replace(' ',''),
                           'version':'Version 1.000','licenseDescription':license_text,
                           'description':'Layout metrics only. Stroke paths are in the adjacent mlfont file. Source: '+item['source_name']})
    builder.setupOS2(sTypoAscender=ascent,sTypoDescender=descent,sTypoLineGap=0,usWinAscent=max(0,ascent),usWinDescent=max(0,-descent))
    builder.setupPost()
    # Deterministic timestamps rather than the time of rebuild.
    builder.font['head'].created=builder.font['head'].modified=2082844800
    builder.font.recalcTimestamp=False
    target=OUTPUT/(item['id']+'-layout.ttf')
    builder.save(target)
    return target


def publish(item,work,ascent,descent,source,credits=''):
    license_file=OUTPUT/(item['id']+'-LICENSE.txt')
    if item.get('license_url'):
        p=download(item['license_url'],CACHE/'sources'/(item['id']+'-LICENSE.txt'),item.get('license_sha256'))
        license_text=p.read_text(encoding=item.get('license_encoding','utf-8-sig'))
        if item['kind']=='hanzi':
            shutil.copyfile(p,OUTPUT/'ARPHICPL.TXT')
    else:
        # EMS originals embed authorship and OFL declaration; retain both.
        generic=OUTPUT/'maoken-LICENSE.txt'
        if not generic.exists():
            first=next(f for f in SOURCE['fonts'] if f['id']=='maoken')
            generic=download(first['license_url'],CACHE/'sources/maoken-LICENSE.txt',first['license_sha256'])
        full=generic.read_text(encoding='utf-8-sig').split('This Font Software',1)[1]
        license_text=credits.strip()+'\n\nThis Font Software'+full
    notice=f'Maliang experimental stroke derivative / layout metrics font.\nSource: {item["source_name"]}\nSource URL: {item["source_url"]}\nSource SHA256: {digest(source)}\nModified: centerline conversion / data format / renamed family. No author endorsement.\n\n'
    license_file.write_text(notice+license_text,encoding='utf-8')
    source_identity={'id':item['id'],'source_name':item['source_name'],'source_url':item['source_url'],
                     'source_sha256':digest(source),'license':item['license'],'ascent':ascent,'descent':descent,
                     'units_per_em':1000,'baseline':0,'y_down':True,'pixel_em':SIZE if item['kind']=='ttf' else None}
    source_identity['method']='skimage-zhang-edge-trails-v2' if item['kind']=='ttf' else 'original-stroke-paths'
    if 'normalization' in item:source_identity['normalization']=item['normalization']
    db=connect(work)
    empty=[cp for cp, in db.execute('SELECT cp FROM glyphs WHERE data=?',(zlib.compress(b'[]',9),))
           if not chr(cp).isspace()]
    source_identity['empty_source_characters']=empty
    source_identity['source_mapping_count']=db.execute('SELECT COUNT(*) FROM glyphs').fetchone()[0]
    # An empty source outline is not drawable coverage. Let another approved
    # font supply it rather than declaring a successful but invisible glyph.
    db.executemany('DELETE FROM glyphs WHERE cp=?',[(cp,) for cp in empty])
    for key,value in source_identity.items():
        db.execute('INSERT OR REPLACE INTO metadata VALUES(?,?)',(key,json.dumps(value,ensure_ascii=False)))
    db.commit()
    rows=db.execute('SELECT cp,advance FROM glyphs ORDER BY cp').fetchall()
    target=OUTPUT/(item['id']+'.mlfont')
    temporary=target.with_suffix('.mlfont.tmp')
    if temporary.exists():temporary.unlink()
    db.execute('VACUUM INTO ?', (str(temporary),))
    db.close();temporary.replace(target)
    proxy=layout_font(item,rows,ascent,descent,notice+license_text)
    result={k:item[k] for k in ('id','name','source_name','license','fallback_ids')}
    if item['kind']=='hanzi':result['original_license_file']='ARPHICPL.TXT'
    result.update(family=item.get('family','Maliang Stroke '+item['id']),file=target.name,layout_file=proxy.name,
                  sha256=digest(target),layout_sha256=digest(proxy),license_file=license_file.name,
                  glyph_count=len(rows),coverage=ranges(cp for cp,_ in rows),ascent=ascent,descent=descent,
                  source_url=item['source_url'],source_sha256=digest(source),conversion=source_identity)
    print(f'Published {item["id"]}: {len(rows)} glyphs, {target.stat().st_size/1024/1024:.2f} MiB',flush=True)
    return result


def main():
    global CACHE,OUTPUT
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workers',type=int,default=max(1,min(6,(os.cpu_count() or 2)-1)))
    parser.add_argument('--only',nargs='*')
    parser.add_argument('--cache',type=Path,default=CACHE)
    parser.add_argument('--output',type=Path,default=OUTPUT)
    parser.add_argument('--limit',type=int,help='Development batch limit; does not publish partial fonts')
    parser.add_argument('--refresh-layouts',action='store_true',help='Rebuild only deterministic metric proxies from completed databases')
    args=parser.parse_args()
    CACHE,OUTPUT=args.cache.resolve(),args.output.resolve()
    OUTPUT.mkdir(parents=True,exist_ok=True);CACHE.mkdir(parents=True,exist_ok=True)
    catalog_path=OUTPUT/'catalog.json'
    existing=json.loads(catalog_path.read_text(encoding='utf-8'))['fonts'] if catalog_path.exists() else []
    entries={f['id']:f for f in existing}
    if args.refresh_layouts:
        for item in SOURCE['fonts']:
            if item['id'] not in entries or (args.only and item['id'] not in args.only):continue
            entry=entries[item['id']]
            with sqlite3.connect(OUTPUT/entry['file']) as db:
                rows=db.execute('SELECT cp,advance FROM glyphs ORDER BY cp').fetchall()
            text=(OUTPUT/entry['license_file']).read_text(encoding='utf-8')
            proxy=layout_font(item,rows,entry['ascent'],entry['descent'],text)
            entry['layout_sha256']=digest(proxy)
            entry['family']=item.get('family','Maliang Stroke '+item['id'])
            if item['kind']=='hanzi':entry['original_license_file']='ARPHICPL.TXT'
        catalog_path.write_text(json.dumps({'format':1,'fonts':list(entries.values())},ensure_ascii=False,indent=2),encoding='utf-8')
        print('Refreshed deterministic layout proxies.',flush=True)
        return
    # Native data first so runtime integration can be tested while TTF work runs.
    fonts=sorted(SOURCE['fonts'],key=lambda f:(f['kind']=='ttf', f['kind']=='hanzi'))
    for item in fonts:
        if args.only and item['id'] not in args.only:continue
        source=source_file(item)
        print('Building '+item['id'],flush=True)
        credits=''
        if item['kind']=='ttf':work,asc,desc=build_ttf(item,source,args.workers,args.limit)
        elif item['kind']=='svg':work,asc,desc,credits=build_svg(item,source)
        else:work,asc,desc=build_hanzi(item,source)
        entries[item['id']]=publish(item,work,asc,desc,source,credits)
        order={f['id']:i for i,f in enumerate(SOURCE['fonts'])}
        document={'format':1,'fonts':sorted(entries.values(),key=lambda f:order[f['id']])}
        catalog_path.write_text(json.dumps(document,ensure_ascii=False,indent=2),encoding='utf-8')
    print('Build complete.',flush=True)


if __name__=='__main__':
    main()
