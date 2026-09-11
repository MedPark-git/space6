"""Populate the artifact-tool-authored XLSX template using standard OOXML.

Each section occupies one continuous merged cell. Print titles repeat the
discussion heading without inserting cells or manual breaks into the body.
"""
from copy import deepcopy
from dataclasses import dataclass
import base64
import datetime as dt
from io import BytesIO
from pathlib import Path
import re
import unicodedata
from xml.etree import ElementTree as ET
from zipfile import ZipFile, ZIP_DEFLATED
from minutes_format import presentation

NS = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
Q = lambda name: '{' + NS + '}' + name
ET.register_namespace('',NS)
TEMPLATE=Path(__file__).parent/'templates/meeting-template.xlsx'


def template_source():
    if TEMPLATE.is_file():
        return TEMPLATE
    encoded=TEMPLATE.with_suffix('.xlsx.b64')
    return BytesIO(base64.b64decode(encoded.read_text().strip(),validate=True))


class ExcelCapacityError(ValueError):
    pass


def continuous_text(value,label):
    """Retain source line breaks and validate Excel's native single-cell limits."""
    value=safe(value).replace('\r\n','\n').replace('\r','\n')
    plain=re.sub(r'\*\*(.+?)\*\*',r'\1',value,flags=re.DOTALL)
    if len(plain.encode('utf-16-le'))//2>32767 or plain.count('\n')>253:
        raise ExcelCapacityError(f'{label}이 엑셀 단일 셀의 한도(32,767자·줄바꿈 253개)를 초과했습니다. 입력한 내용은 그대로 보존됩니다. 내용을 정리한 뒤 다시 다운로드해 주세요.')
    return value


def safe(value):
    return ''.join(ch for ch in str(value) if ch in '\n\r\t' or ord(ch)>=32)


def width(value):
    return sum(2 if unicodedata.east_asian_width(ch) in ('W','F') else 1 for ch in value)


def lines(value, limit=120):
    result=[]
    for line in safe(value).replace('\r\n','\n').replace('\r','\n').split('\n'):
        if not line:
            result.append('')
            continue
        current=''
        count=0
        for char in line:
            size=2 if unicodedata.east_asian_width(char) in ('W','F') else 1
            if count+size>limit:
                result.append(current)
                current=''
                count=0
            current+=char
            count+=size
        result.append(current)
    return result or ['']


@dataclass(frozen=True)
class StyledText:
    """Text runs retain emphasis when a section spans rows or row chunks."""
    runs: tuple


def emphasis_lines(value, limit=120):
    """Wrap only paired **emphasis**; other text and unmatched stars stay literal.

    Parse before wrapping so a bold span may cross both newlines and the
    physical rows used by the Excel export. ``lines`` stays unchanged for
    metadata and callers that require ordinary strings.
    """
    value=safe(value).replace('\r\n','\n').replace('\r','\n')
    runs=[]
    previous=0
    for match in re.finditer(r'\*\*(.+?)\*\*',value,re.DOTALL):
        if match.start()>previous:
            runs.append((value[previous:match.start()],False))
        runs.append((match.group(1),True))
        previous=match.end()
    if previous<len(value):
        runs.append((value[previous:],False))
    result=[]
    current=[]
    count=0

    def finish():
        nonlocal current,count
        result.append(StyledText(tuple(current)))
        current=[]
        count=0

    for text,bold in runs:
        for char in text:
            if char=='\n':
                finish()
                continue
            size=2 if unicodedata.east_asian_width(char) in ('W','F') else 1
            if count+size>limit:
                finish()
            if current and current[-1][1]==bold:
                current[-1]=(current[-1][0]+char,bold)
            else:
                current.append((char,bold))
            count+=size
    finish()
    return result


def join_emphasis(rows):
    runs=[]
    for index,row in enumerate(rows):
        if index:
            runs.append(('\n',False))
        runs.extend(row.runs)
    return StyledText(tuple(runs))


def put(cell,value, numeric=False):
    for child in list(cell):
        cell.remove(child)
    if numeric:
        cell.attrib.pop('t',None)
        ET.SubElement(cell,Q('v')).text=str(value)
    else:
        cell.set('t','inlineStr')
        inline=ET.SubElement(cell,Q('is'))
        if isinstance(value,StyledText) and any(bold for _,bold in value.runs):
            for content,bold in value.runs:
                if not content:
                    continue
                run=ET.SubElement(inline,Q('r'))
                if bold:
                    ET.SubElement(ET.SubElement(run,Q('rPr')),Q('b'))
                text=ET.SubElement(run,Q('t'))
                text.set('{http://www.w3.org/XML/1998/namespace}space','preserve')
                text.text=content
        else:
            text=ET.SubElement(inline,Q('t'))
            text.set('{http://www.w3.org/XML/1998/namespace}space','preserve')
            text.text=''.join(content for content,_ in value.runs) if isinstance(value,StyledText) else safe(value)


def export_meeting(data):
    data=presentation(data)
    discussion_text=continuous_text(data.get('discussion',''),'회의내용')
    notes_text=continuous_text(data.get('notes',''),'특이사항')
    with ZipFile(template_source()) as zin:
        parts={name:zin.read(name) for name in zin.namelist()}
    sheet=ET.fromstring(parts['xl/worksheets/sheet1.xml'])
    strings=[]
    if 'xl/sharedStrings.xml' in parts:
        strings=[''.join(si.itertext()) for si in ET.fromstring(parts['xl/sharedStrings.xml']).findall(Q('si'))]
    rows=sheet.find(Q('sheetData'))
    prototypes={int(row.attrib['r']):deepcopy(row) for row in rows}
    old_merges=[m.attrib['ref'] for m in sheet.find(Q('mergeCells'))]
    for child in list(rows):
        rows.remove(child)
    merges=sheet.find(Q('mergeCells'))
    for child in list(merges):
        merges.remove(child)
    values={'{{title}}':data['title'], '{{meeting_date}}':data['meeting_date'],
        '{{duration}}':data.get('duration',''), '{{author}}':data.get('author',''),
        '{{reporter}}':data.get('reporter',''), '{{attendees}}':', '.join(data.get('attendees',[]))}
    styles=ET.fromstring(parts['xl/styles.xml'])
    numfmts=styles.find(Q('numFmts'))
    if numfmts is None:
        numfmts=ET.Element(Q('numFmts'),{'count':'0'})
        styles.insert(0,numfmts)
    used={int(n.get('numFmtId','0')) for n in numfmts}
    fmtid=max([163,*used])+1
    ET.SubElement(numfmts,Q('numFmt'),{'numFmtId':str(fmtid),'formatCode':'yyyy-mm-dd'})
    numfmts.set('count',str(len(numfmts)))
    xfs=styles.find(Q('cellXfs'))
    date_xf=deepcopy(xfs[int(prototypes[5].find("./"+Q('c')+"[@r='C5']").get('s','0'))])
    date_xf.set('numFmtId',str(fmtid));date_xf.set('applyNumberFormat','1')
    date_style=len(xfs);xfs.append(date_xf);xfs.set('count',str(len(xfs)))
    index=0
    discussion_header=0
    block_styles={}
    borders=styles.find(Q('borders'))

    def block_style(top,bottom,left,right):
        key=(top,bottom,left,right)
        if key not in block_styles:
            border=ET.Element(Q('border'))
            for edge,enabled in [('left',left),('right',right),('top',top),('bottom',bottom)]:
                if enabled:
                    ET.SubElement(ET.SubElement(border,Q(edge),{'style':'thin'}),Q('color'),{'rgb':'FFB7C9AF'})
            style=deepcopy(xfs[33]);style.set('borderId',str(len(borders)))
            borders.append(border)
            alignment=style.find(Q('alignment'))
            alignment.set('vertical','top');alignment.set('horizontal','left');alignment.set('wrapText','1')
            block_styles[key]=len(xfs);xfs.append(style)
        return block_styles[key]

    def append_row(source, replacements=None, height=None, add_merges=True):
        nonlocal index
        index+=1
        row=deepcopy(prototypes[source]);row.set('r',str(index))
        if height:
            row.set('ht',str(min(390,height)));row.set('customHeight','1')
        for cell in row.findall(Q('c')):
            col=re.sub(r'\d','',cell.get('r'))
            cell.set('r',col+str(index))
            if replacements is not None and col in replacements:
                put(cell,replacements[col]);continue
            if cell.get('t')=='s':
                v=cell.find(Q('v'));content=strings[int(v.text)] if v is not None else ''
            elif cell.get('t')=='str':
                v=cell.find(Q('v'));content=v.text or '' if v is not None else ''
            else:
                content=''.join(cell.find(Q('is')).itertext()) if cell.find(Q('is')) is not None else ''
            if content in values:
                value=values[content]
                if content=='{{meeting_date}}':
                    number=(dt.date.fromisoformat(value)-dt.date(1899,12,30)).days
                    put(cell,number,True);cell.set('s',str(date_style))
                elif content=='{{duration}}' and isinstance(value,int):
                    put(cell,value,True)
                else:
                    put(cell,value)
            elif '{{' in content:
                put(cell,'')
        rows.append(row)
        for merge in old_merges if add_merges else []:
            a,b=merge.split(':')
            if int(re.search(r'\d+',a).group())==source:
                ca=re.sub(r'\d','',a);cb=re.sub(r'\d','',b)
                ET.SubElement(merges,Q('mergeCell'),{'ref':f'{ca}{index}:{cb}{index}'})
        return row

    def merged_block(source,chunk,line_count=None):
        # One merged value, supported by small rows. Align native automatic
        # page boundaries to the 11 pt text lines instead of page-sized boxes.
        count=max(1,line_count if line_count is not None else len(chunk))
        first=index+1
        for offset in range(count):
            row_height=24 if count==1 else 15.2 if offset==0 else 20.4 if offset==count-1 else 14.4
            row=append_row(source,{'A':join_emphasis(chunk) if offset==0 else ''},
                row_height,add_merges=False)
            for cell in row.findall(Q('c')):
                col=re.sub(r'\d','',cell.get('r'))
                cell.set('s',str(block_style(offset==0,offset==count-1,col=='A',col=='L')))
        ET.SubElement(merges,Q('mergeCell'),{'ref':f'A{first}:L{index}'})

    def text_section(spacer,header,prototype,title,text):
        nonlocal discussion_header
        wrapped=emphasis_lines(text,84)
        logical=emphasis_lines(text,10**9)
        if title=='회의내용':
            logical=[StyledText(tuple((t,True) for t,_ in line.runs))
                if re.match(r'^\d{1,2}\.\s',''.join(t for t,_ in line.runs)) else line
                for line in logical]
        append_row(spacer);append_row(header,{'A':title})
        if title=='회의내용':discussion_header=index
        merged_block(prototype,logical,len(wrapped))

    for n in range(1,10):
        height=None
        if n==3:height=max(36,len(lines(data['title'],110))*21+12)
        if n==6:height=max(30,len(lines(data.get('author',''),40))*18+12,len(lines(data.get('reporter',''),40))*18+12)
        if n==7:height=max(30,len(lines(values['{{attendees}}'],100))*18+12)
        append_row(n,height=height)
    conclusions=data.get('conclusions',[])
    for i in range(max(1,len(conclusions))):
        item=conclusions[i] if i<len(conclusions) else ''
        wrapped=emphasis_lines(item,70)
        append_row(11,{'A':str(i+1) if item else '', 'C':join_emphasis(wrapped)},
            max(29,len(wrapped)*18+10))
    text_section(16,17,18,'회의내용',discussion_text)
    text_section(19,20,21,'특이사항',notes_text)
    merges.set('count',str(len(merges)))
    dimension=sheet.find(Q('dimension'))
    if dimension is not None:dimension.set('ref',f'A1:L{index}')
    pr=sheet.find(Q('sheetPr'))
    if pr is None:pr=ET.Element(Q('sheetPr'));sheet.insert(0,pr)
    setuppr=pr.find(Q('pageSetUpPr'))
    if setuppr is None:setuppr=ET.SubElement(pr,Q('pageSetUpPr'))
    setuppr.set('fitToPage','0')
    for tag in ('printOptions','pageMargins','pageSetup'):
        existing=sheet.find(Q(tag))
        if existing is not None:sheet.remove(existing)
    ET.SubElement(sheet,Q('printOptions'),{'horizontalCentered':'1'})
    ET.SubElement(sheet,Q('pageMargins'),{'left':'0.3','right':'0.3','top':'0.4','bottom':'0.4','header':'0.2','footer':'0.2'})
    ET.SubElement(sheet,Q('pageSetup'),{'paperSize':'9','orientation':'portrait','scale':'100','fitToWidth':'1','fitToHeight':'0'})
    old_breaks=sheet.find(Q('rowBreaks'))
    if old_breaks is not None:sheet.remove(old_breaks)
    xfs.set('count',str(len(xfs)));borders.set('count',str(len(borders)))
    parts['xl/styles.xml']=ET.tostring(styles,encoding='utf-8',xml_declaration=True)
    parts['xl/worksheets/sheet1.xml']=ET.tostring(sheet,encoding='utf-8',xml_declaration=True)
    book=ET.fromstring(parts['xl/workbook.xml'])
    names=book.find(Q('definedNames'))
    if names is None:names=ET.SubElement(book,Q('definedNames'))
    for node in list(names):
        if node.get('name') in ('_xlnm.Print_Area','_xlnm.Print_Titles'):names.remove(node)
    ET.SubElement(names,Q('definedName'),{'name':'_xlnm.Print_Area','localSheetId':'0'}).text=f"'회의록'!$A$1:$L${index}"
    ET.SubElement(names,Q('definedName'),{'name':'_xlnm.Print_Titles','localSheetId':'0'}).text=f"'회의록'!${discussion_header}:${discussion_header}"
    parts['xl/workbook.xml']=ET.tostring(book,encoding='utf-8',xml_declaration=True)
    output=BytesIO()
    with ZipFile(output,'w',ZIP_DEFLATED) as zout:
        for name,value in parts.items():zout.writestr(name,value)
    return output.getvalue()
