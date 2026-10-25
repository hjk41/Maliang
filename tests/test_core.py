import json
import os
import tempfile
import unittest
from pathlib import Path

# Windows' offscreen font backend yields tofu and crashes QRawFont. Native Qt
# windows are never shown in these tests; real installed glyphs are exercised.
os.environ["QT_QPA_PLATFORM"] = "windows" if os.name == "nt" else "offscreen"
from PySide6.QtCore import QPointF
from PySide6.QtGui import QPainterPath
from PySide6.QtWidgets import QApplication
from pagewriter.core import *

app = QApplication.instance() or QApplication([])


class CoreTests(unittest.TestCase):
    def test_mixed_styles_generate_shared_curves(self):
        b = TextBlock(html='<p style="font-family:Arial;font-size:12pt;">small <span style="font-size:24pt;font-family:Times New Roman;">BIG</span></p>')
        path = local_block_path(b)
        self.assertGreater(path.elementCount(), 100)
        self.assertGreater(path.boundingRect().height(), 5)
        self.assertGreaterEqual(len(b.fonts), 2)
        self.assertEqual(path_to_data(path), path_to_data(local_block_path(b)))
        self.assertIn("C ", svg_path_data(path))

    def test_portable_project_exact_path_and_image(self):
        p = starter_project()
        before = [path_to_data(build_block_path(b)) for b in p.blocks]
        with tempfile.TemporaryDirectory() as d:
            file = Path(d) / 'example.pagewriter'
            save_project(p, file)
            loaded = load_project(file)
        self.assertEqual(p.image_data, loaded.image_data)
        self.assertEqual(p.blocks[0].html, loaded.blocks[0].html)
        self.assertEqual(before, [path_to_data(build_block_path(b)) for b in loaded.blocks])

    def test_demo_clear_exports_exact_path(self):
        p = starter_project()
        self.assertEqual([], preflight(p))
        svg = export_svg(p)
        self.assertIn(svg_path_data(build_block_path(p.blocks[0])), svg)
        self.assertIn('width="148.0mm"', svg)
        preview = offline_gcode(p)
        self.assertTrue(all(line.startswith(';') for line in preview.splitlines()))
        self.assertNotIn(' Z', preview)

    def test_boundary_and_polygon_collisions_block(self):
        p = Project(blocks=[TextBlock(html='<p style="font-size:20pt;">Hello</p>', x=20, y=20, avoid_zones=False)])  # tests the safety check, not flow-around
        path = build_block_path(p.blocks[0])
        r = path.boundingRect()
        p.zones = [ExclusionZone(points=[(r.left(), r.top()), (r.right(), r.top()), (r.center().x(), r.bottom())])]
        self.assertTrue(preflight(p))
        with self.assertRaises(ExportBlocked):
            export_svg(p)
        with self.assertRaises(ExportBlocked):
            offline_gcode(p)
        p.zones = []
        p.blocks[0].x = -100
        self.assertTrue(any(c.zone_id == 'page' for c in preflight(p)))

    def test_pen_margin_is_geometric_not_box(self):
        p = Project(pen_width=.4, margin=.3)
        b = TextBlock(x=10,y=10,avoid_zones=False)
        line = QPainterPath(QPointF(0,0)); line.lineTo(10,0)
        b.frozen_key = __import__('hashlib').sha256((b.html+'\0'+str(float(b.width))).encode()).hexdigest()
        b.frozen_path = path_to_data(line)
        p.blocks=[b]
        p.zones=[ExclusionZone(points=[(11,10.49),(19,10.49),(19,12),(11,12)])]
        self.assertTrue(preflight(p))
        p.zones[0].points=[(11,10.51),(19,10.51),(19,12),(11,12)]
        self.assertEqual([],preflight(p))

    def test_zoom_has_no_input_to_geometry(self):
        p = starter_project()
        before=export_svg(p)
        # Any screen/view transforms are deliberately outside the document.
        from PySide6.QtGui import QTransform
        for scale in (.25, 1, 4, 20):
            QTransform.fromScale(scale,scale).map(build_block_path(p.blocks[0]))
        self.assertEqual(before,export_svg(p))

    def test_move_rotate_preserves_local_frozen_shape(self):
        b=TextBlock()
        before=path_to_data(local_block_path(b))
        b.x=37;b.y=54;b.rotation=90
        after=build_block_path(b)
        expected=block_transform(b).map(path_from_data(before))
        self.assertEqual(path_to_data(expected),path_to_data(after))

    def test_obstacle_blocks_pen_up(self):
        p=Project(blocks=[TextBlock(x=10,y=20,html='A'),TextBlock(x=90,y=20,html='B')],zones=[ExclusionZone(kind='obstacle',points=[(50,0),(60,0),(60,100),(50,100)])])
        self.assertTrue(any('抬笔' in c.message for c in preflight(p)))

    def test_chinese_multiline_baselines_and_font_warning(self):
        b=TextBlock(html='<p style="font-family:Microsoft YaHei;font-size:12pt;">甲乙<br/>丙丁</p>')
        path=local_block_path(b)
        self.assertGreater(path.elementCount(),100)
        self.assertGreater(path.boundingRect().height(),8)
        self.assertFalse(any(f.get('missing_glyphs') for f in b.fonts))
        b.html='<p style="font-family:PageWriter_Deliberately_Missing_12345;">hello</p>'
        self.assertTrue(font_warnings(Project(blocks=[b])))

    def test_adaptive_curve_flattening_handles_loop_and_endpoints(self):
        p=QPainterPath(QPointF(0,0));p.cubicTo(10,20,-10,20,0,0)
        paths=path_subpaths(p)
        self.assertEqual(paths[0][0],(0,0))
        self.assertEqual(paths[0][-1],(0,0))
        self.assertGreater(len(paths[0]),20)
        for i in range(101):
            t=i/100
            x=30*(1-t)**2*t-30*(1-t)*t*t
            y=60*(1-t)*t
            distances=[]
            for a,b in zip(paths[0],paths[0][1:]):
                dx,dy=b[0]-a[0],b[1]-a[1]
                ratio=max(0,min(1,((x-a[0])*dx+(y-a[1])*dy)/(dx*dx+dy*dy)))
                distances.append(__import__('math').hypot(x-a[0]-ratio*dx,y-a[1]-ratio*dy))
            self.assertLessEqual(min(distances),.020001)

    def test_reject_nonfinite_and_malformed_project_geometry(self):
        import copy
        p=Project(zones=[ExclusionZone(points=[(0,0),(1,0),(1,1)])])
        payload=project_to_dict(p)
        bad=copy.deepcopy(payload);bad['project']['zones'][0]['points'][0]=[float('nan'),0]
        with self.assertRaises(ValueError):project_from_dict(bad)
        for points in ([],[(0,0),(1,1)],[(0,0),(1,0),(1,float('inf'))],[(0,0,1),(1,0),(1,1)]):
            bad=copy.deepcopy(payload);bad['project']['zones'][0]['points']=points
            with self.assertRaises(ValueError):project_from_dict(bad)
        for field,value in [('image_offset',[float('nan'),0]),('image_offset',[0]),('image_quad',[[0,0],[1,0],[1,1]]),('image_quad',[[0,0],[1,0],[1,1],[0,float('inf')]])]:
            bad=copy.deepcopy(payload);bad['project'][field]=value
            with self.assertRaises(ValueError):project_from_dict(bad)
        for version in (True,2,'1'):
            bad=copy.deepcopy(payload);bad['version']=version
            with self.assertRaises(ValueError):project_from_dict(bad)

    def test_reject_nonfinite_and_invalid_cached_path_structure(self):
        invalid=(
            [[0,float('nan'),0]], [[1,0,0]], [[3,0,0]],
            [[0,0,0],[2,1,1]],
            [[0,0,0],[2,1,1],[3,2,2],[1,3,3]],
            [[0,0,0],[3,1,1]],
            [[0,0]], [[True,0,0]], [[4,0,0]],
        )
        for elements in invalid:
            with self.subTest(elements=elements):
                with self.assertRaises(ValueError):path_from_data(elements)
        payload=project_to_dict(Project(blocks=[TextBlock()]))
        payload['project']['blocks'][0]['frozen_key']='stale'
        payload['project']['blocks'][0]['frozen_path']=[[0,0,float('inf')]]
        with self.assertRaises(ValueError):project_from_dict(payload)

    def test_direct_invalid_geometry_also_blocks_preflight(self):
        p=Project(zones=[ExclusionZone(points=[(0,0),(1,0),(float('nan'),1)])])
        self.assertTrue(preflight(p))
        with self.assertRaises(ExportBlocked):export_svg(p)

    def test_columns_flow_preserves_glyph_curves_and_order(self):
        html='<p style="font-family:Arial;font-size:12pt;">A<br/>B<br/>C<br/>D<br/>E<br/>F</p>'
        single=TextBlock(html=html,width=30)
        mult=TextBlock(html=html,width=102,columns=3,column_height=13,column_gap=6)
        a,b=local_block_path(single),local_block_path(mult)
        info=layout_info(mult)
        self.assertEqual([0,0,1,1,2,2],[line['column'] for line in info['lines']])
        self.assertFalse(info['overflow'])
        self.assertEqual(a.elementCount(),b.elementCount())
        self.assertEqual([a.elementAt(i).type for i in range(a.elementCount())],[b.elementAt(i).type for i in range(b.elementCount())])
        # Every subpath's relative cubic control points remain identical; only
        # its placement changes. This detects both missing and reordered glyphs.
        def relative_curves(path):
            result=[];ox=oy=0
            for i in range(path.elementCount()):
                e=path.elementAt(i)
                if e.isMoveTo():ox,oy=e.x,e.y
                result.append((e.type.value,round(e.x-ox,7),round(e.y-oy,7)))
            return result
        self.assertEqual(relative_curves(a),relative_curves(b))
        self.assertEqual([0,2,4,6,8,10],[line['start'] for line in info['lines']])

    def test_columns_cache_reflows_and_portable_fields(self):
        html='<p style="font-family:Microsoft YaHei;font-size:12pt;">甲乙丙丁<span style="font-size:19pt;">大小混排</span>戊己庚辛<br/>下一行批注</p>'
        b=TextBlock(html=html,width=100,columns=2,column_height=15,column_gap=6)
        before=path_to_data(local_block_path(b));oldkey=b.frozen_key
        b.column_gap=12
        self.assertNotEqual(__import__('hashlib').sha256(repr(before).encode()).hexdigest(),__import__('hashlib').sha256(repr(path_to_data(local_block_path(b))).encode()).hexdigest());self.assertNotEqual(oldkey,b.frozen_key)
        oldkey=b.frozen_key;b.column_height=30
        local_block_path(b);self.assertNotEqual(oldkey,b.frozen_key)
        oldkey=b.frozen_key;b.columns=3
        local_block_path(b);self.assertNotEqual(oldkey,b.frozen_key)
        p=Project(blocks=[b]);q=project_from_dict(project_to_dict(p));loaded=q.blocks[0]
        self.assertEqual((3,30,12),(loaded.columns,loaded.column_height,loaded.column_gap))
        self.assertEqual(path_to_data(local_block_path(b)),path_to_data(local_block_path(loaded)))
        self.assertEqual(layout_info(b),layout_info(loaded))

    def test_third_column_zone_and_strict_overflow_export(self):
        b=TextBlock(html='<p style="font-family:Arial;font-size:12pt;">A<br/>B<br/>C</p>',width=102,columns=3,column_height=8,column_gap=6,x=10,y=10,avoid_zones=False)
        p=Project(blocks=[b],width=148,height=210)
        self.assertEqual([],preflight(p))
        p.zones=[ExclusionZone(points=[(82,10),(110,10),(110,18),(82,18)])]
        self.assertTrue(any(c.zone_id=='zone-1' for c in preflight(p)))
        p.zones=[];b.html+='<p>overflow</p>'
        self.assertTrue(any(c.zone_id=='overflow' for c in preflight(p)))
        with self.assertRaises(ExportBlocked):export_svg(p)
        with self.assertRaises(ExportBlocked):offline_gcode(p)

    def test_columns_whitespace_tall_line_and_too_narrow_reject(self):
        cases=[
            TextBlock(html='<p><br/><br/><br/><br/><br/><br/><br/><br/></p>',columns=2,column_height=10),
            TextBlock(html='<p style="font-size:100pt;">A</p>',columns=2,column_height=5),
            TextBlock(html='<p style="font-size:30pt;">W</p>',width=7,columns=2,column_height=60,column_gap=6),
        ]
        for b in cases:
            with self.subTest(b=b):
                self.assertTrue(layout_info(b)['overflow'])
                self.assertTrue(any(c.zone_id=='overflow' for c in preflight(Project(blocks=[b]))))

    def test_mixed_size_line_height_and_old_frozen_v1_exact(self):
        b=TextBlock(html='<p style="font-family:Arial;font-size:10pt;">A<br/><span style="font-size:30pt;">B</span><br/>C</p>',width=80,columns=2,column_height=25)
        info=layout_info(b)
        self.assertGreater(info['lines'][1]['height'],info['lines'][0]['height']*2)
        original=TextBlock(html='<p>Old v1 frozen</p>')
        expected=path_to_data(local_block_path(original))
        payload=project_to_dict(Project(blocks=[original]));stored=payload['project']['blocks'][0]
        for key in ('columns','column_height','column_gap','frozen_overflow','frozen_layout'):stored.pop(key)
        loaded=project_from_dict(payload).blocks[0]
        self.assertEqual(expected,path_to_data(local_block_path(loaded)))
        self.assertEqual(original.frozen_key,loaded.frozen_key)

    def test_partial_layout_cache_uses_authoritative_geometry_without_reshaping(self):
        from unittest.mock import patch
        b=TextBlock(html='<p>Preserve cached font outlines</p>')
        before=path_to_data(local_block_path(b))
        b.frozen_layout={'overflow':False}
        loaded=project_from_dict(project_to_dict(Project(blocks=[b]))).blocks[0]
        with patch('pagewriter.core.make_document',side_effect=AssertionError('must not reshape')):
            info=layout_info(loaded)
            self.assertEqual(65,info['column_width'])
            self.assertEqual(1,info['columns'])
            self.assertEqual(0,info['height'])
            self.assertEqual(before,path_to_data(local_block_path(loaded)))
            loaded.frozen_layout.update(column_width=999,height=999,gap=999,columns=3)
            info=layout_info(loaded)
            self.assertEqual((65,0,6,1),(info['column_width'],info['height'],info['gap'],info['columns']))

    def test_malformed_layout_cache_stats_and_line_geometry_rejected(self):
        import copy
        payload=project_to_dict(Project(blocks=[TextBlock()]))
        invalid=(
            {'overflow':'false'}, {'overflow':True}, {'line_count':True},
            {'page_count':0}, {'lines':'bad'}, {'lines':[[]]},
            {'column_width':float('nan')}, {'height':'60'},
            {'line_count':1,'lines':[]},
            {'page_count':2,'lines':[]},
            {'lines':[{'start':0,'length':1,'column':0,'y':float('inf'),'height':1,'width':1}]},
            {'lines':[{'start':0,'length':1,'column':0,'y':0,'height':1,'width':'1'}]},
            {'lines':[{'start':0,'length':True,'column':0,'y':0,'height':1,'width':1}]},
        )
        for cache in invalid:
            with self.subTest(cache=cache):
                bad=copy.deepcopy(payload);bad['project']['blocks'][0]['frozen_layout']=cache
                with self.assertRaises(ValueError):project_from_dict(bad)

    def test_partial_underline_exact_selected_width_and_baseline(self):
        b=TextBlock(html='<p style="font-family:Arial;font-size:20pt;">A<span style="text-decoration:underline;">bc</span>D</p>')
        path=local_block_path(b)
        spans=b.frozen_layout['underlines']
        self.assertEqual(1,len(spans));span=spans[0]
        self.assertEqual((1,3),(span['start'],span['end']))
        doc=make_document(b);paragraph=doc.begin();line=paragraph.layout().lineAt(0)
        self.assertAlmostEqual(line.cursorToX(1)[0]*MM_PER_PX,span['x1'])
        self.assertAlmostEqual(line.cursorToX(3)[0]*MM_PER_PX,span['x2'])
        self.assertGreater(span['y'],line.ascent()*MM_PER_PX)
        # The decoration is one real pen centerline, not an outlined rectangle.
        last=path.elementAt(path.elementCount()-1);previous=path.elementAt(path.elementCount()-2)
        self.assertTrue(previous.isMoveTo());self.assertTrue(last.isLineTo())
        self.assertAlmostEqual(previous.y,last.y)

    def test_underline_mixed_sizes_multiline_columns_and_utf16(self):
        b=TextBlock(html='<p style="font-family:Arial;font-size:12pt;"><u>A<br/>B<br/>C<br/>D</u></p>',width=86,columns=2,column_height=13,column_gap=6)
        local_block_path(b)
        spans=b.frozen_layout['underlines']
        self.assertEqual([0,0,1,1],[s['column'] for s in spans])
        self.assertTrue(all(s['x2']-s['x1']<10 for s in spans))
        self.assertTrue(all(0<s['y']<13 for s in spans))
        self.assertGreater(spans[2]['x1'],40)
        mixed=TextBlock(html='<p style="font-family:Arial;font-size:12pt;"><u>g</u><span style="font-size:24pt;text-decoration:underline;">Q</span></p>')
        local_block_path(mixed);self.assertEqual(2,len(mixed.frozen_layout['underlines']))
        self.assertNotEqual(mixed.frozen_layout['underlines'][0]['y'],mixed.frozen_layout['underlines'][1]['y'])
        utf=TextBlock(html='<p>😀<u>A😀B</u>C</p>')
        local_block_path(utf)
        self.assertEqual([(2,6)],[(s['start'],s['end']) for s in utf.frozen_layout['underlines']])

    def test_underline_whitespace_and_inherited_style(self):
        b=TextBlock(html='<p style="text-decoration:underline;font-family:Arial;white-space:pre-wrap;">A  B<br/>C</p>')
        local_block_path(b)
        self.assertEqual([(0,4),(5,6)],[(s['start'],s['end']) for s in b.frozen_layout['underlines']])
        plain=TextBlock(html='<p><span style="text-decoration:none;">underline is just a word</span></p>')
        local_block_path(plain)
        expected=__import__('hashlib').sha256((plain.html+'\0'+str(float(plain.width))).encode()).hexdigest()
        self.assertEqual(expected,plain.frozen_key)

    def test_underline_key_invalidates_legacy_glyph_only_cache(self):
        b=TextBlock(html='<p><u>Decorated</u></p>')
        # Reproduce a pre-decoration saved project with the old hash and a
        # deliberately empty glyph cache. New code must generate its underline.
        b.frozen_key=__import__('hashlib').sha256((b.html+'\0'+str(float(b.width))).encode()).hexdigest()
        old=b.frozen_key
        self.assertGreater(local_block_path(b).elementCount(),0)
        self.assertNotEqual(old,b.frozen_key)
        self.assertEqual(1,len(b.frozen_layout['underlines']))

    def test_separator_centers_single_ignore_and_frozen_font_reuse(self):
        from unittest.mock import patch
        b=TextBlock(html='<p>Frozen</p>',columns=3,column_height=40,width=102,column_gap=6)
        before=path_to_data(local_block_path(b));key=b.frozen_key
        b.column_separators=True
        with patch('pagewriter.core.make_document',side_effect=AssertionError('no font reshaping')):
            lines=separator_path(b)
            self.assertEqual(4,lines.elementCount())
            self.assertEqual([33,33,69,69],[lines.elementAt(i).x for i in range(4)])
            self.assertEqual([0,40,0,40],[lines.elementAt(i).y for i in range(4)])
            composed=composed_local_path(b)
            self.assertEqual(len(before)+4,composed.elementCount())
            self.assertEqual(before,path_to_data(local_block_path(b)));self.assertEqual(key,b.frozen_key)
        b.columns=1
        self.assertTrue(separator_path(b).isEmpty())

    def test_separator_and_underline_collisions_export_and_roundtrip(self):
        b=TextBlock(html='',x=20,y=20,width=100,columns=2,column_height=50,column_gap=6,column_separators=True,avoid_zones=False)
        p=Project(blocks=[b]);self.assertEqual([],preflight(p))
        svg=export_svg(p);self.assertIn('M 70.000000000 20.000000000 L 70.000000000 70.000000000',svg)
        self.assertEqual([],local_block_path(b).toSubpathPolygons())
        p.zones=[ExclusionZone(points=[(69.9,35),(70.1,35),(70.1,36),(69.9,36)])]
        self.assertTrue(preflight(p))
        with self.assertRaises(ExportBlocked):export_svg(p)
        p.zones=[]
        loaded=project_from_dict(project_to_dict(p));self.assertTrue(loaded.blocks[0].column_separators)
        self.assertEqual(path_to_data(build_block_path(b)),path_to_data(build_block_path(loaded.blocks[0])))
        u=TextBlock(html='<p><u>test</u></p>',x=20,y=20,avoid_zones=False)
        local_block_path(u);segment=u.frozen_layout['underlines'][0]
        x=(segment['x1']+segment['x2'])/2+20;y=segment['y']+20
        q=Project(blocks=[u],pen_width=.2,margin=.3,zones=[ExclusionZone(points=[(x-.05,y+.39),(x+.05,y+.39),(x+.05,y+.45),(x-.05,y+.45)])])
        self.assertTrue(preflight(q))

if __name__ == '__main__':
    unittest.main()
