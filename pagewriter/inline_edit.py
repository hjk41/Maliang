"""Canvas-native rich text interaction using the exact paginated Qt layout."""
import math
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QPen, QTextCursor, QTextLayout, QTextCharFormat, QFont, QInputMethodEvent
from .core import layout_lines, PX_PER_MM, MM_PER_PX


def lines_for(block):
    """Same line placement as the strokes, including flow around protected zones."""
    doc, geometry, entries, _ = layout_lines(block)
    height = geometry['page_height_px']
    result = []
    for paragraph, line, origin in entries:
        top = origin.y() + line.y()
        column = max(0, int(math.floor((top + 1e-6)/height))) if height else 0
        x = column * (geometry['column_width'] + block.column_gap)
        y = (top - column*height) * MM_PER_PX if height else top*MM_PER_PX
        result.append((paragraph.position(), line, x, y, origin.x()))
    return doc, result


def hit_position(block, point):
    doc, lines = lines_for(block)
    if not lines:
        return 0
    def distance(entry):
        _, line, x, y, ox = entry
        left = x + (ox + line.x())*MM_PER_PX  # flowed lines may start right of the column edge
        right = left + max(line.naturalTextWidth()*MM_PER_PX, 1)
        return max(left-point.x(), 0, point.x()-right)**2 + max(y-point.y(),0,point.y()-y-line.height()*MM_PER_PX)**2
    pos, line, x, y, ox = min(lines, key=distance)
    local_x = (point.x()-x)*PX_PER_MM-ox
    return min(doc.characterCount()-1, pos+line.xToCursor(local_x))


def cursor_geometry(block, position):
    """One caret at a soft wrap belongs to the following visual line."""
    doc, lines = lines_for(block)
    matches=[entry for entry in lines if entry[0]+entry[1].textStart() <= position <= entry[0]+entry[1].textStart()+entry[1].textLength()]
    if not matches:return None
    pos,line,x,y,ox=matches[-1]
    value=line.cursorToX(position-pos)
    px=x+(ox+(value[0] if isinstance(value,tuple) else value))*MM_PER_PX
    return QRectF(px,y,.3,line.height()*MM_PER_PX)


def preedit_layout(canvas, editor):
    font=editor.textCursor().charFormat().font()
    if font.pointSizeF()<=0:font=QFont(editor.document().defaultFont())
    layout=QTextLayout(canvas.preedit_text,font)
    ranges=[]
    default=QTextLayout.FormatRange();default.start=0;default.length=len(canvas.preedit_text)
    default.format=QTextCharFormat();default.format.setFontUnderline(True);default.format.setForeground(QColor('#162e55'))
    ranges.append(default)
    for attr in canvas.preedit_attributes:
        if attr.type==QInputMethodEvent.AttributeType.TextFormat:
            r=QTextLayout.FormatRange();r.start=attr.start;r.length=attr.length
            r.format=QTextCharFormat(attr.value);ranges.append(r)
    layout.setFormats(ranges)
    layout.beginLayout();line=layout.createLine();line.setLineWidth(10000);layout.endLayout()
    return layout


def paint_cursor(item, painter):
    window = item.window
    if getattr(window.canvas, 'editing_id', None) != item.block_id:return
    block = window.block_by_id(item.block_id)
    cursor = window.editor.textCursor()
    doc, lines = lines_for(block)
    start, end = cursor.selectionStart(), cursor.selectionEnd()
    painter.save()
    for pos, line, x, y, ox in lines:
        lo, hi = pos+line.textStart(), pos+line.textStart()+line.textLength()
        def xpos(index):
            value = line.cursorToX(index-pos)
            return x+(ox+(value[0] if isinstance(value,tuple) else value))*MM_PER_PX
        if start < hi and end > lo:
            left, right = xpos(max(start,lo)), xpos(min(end,hi))
            painter.fillRect(QRectF(left,y,max(.2,right-left),line.height()*MM_PER_PX),QColor(60,120,230,70))
    rect=cursor_geometry(block,cursor.position())
    if rect:
        painter.setPen(QPen(QColor('#162e55'), .23))
        painter.drawLine(rect.topLeft(),rect.bottomLeft())
        if window.canvas.preedit_text:
            layout=preedit_layout(window.canvas,window.editor)
            painter.translate(rect.topLeft());painter.scale(MM_PER_PX,MM_PER_PX)
            painter.fillRect(QRectF(0,0,layout.boundingRect().width(),layout.boundingRect().height()),QColor(255,255,255,235))
            layout.draw(painter,QPointF(0,0))
    painter.restore()
