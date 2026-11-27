"""Fixed OOXML validator for newly uploaded execution outputs; never runs document code."""
from hashlib import sha256
from io import BytesIO
import json
from math import sqrt
from pathlib import PurePosixPath
import sys
from zipfile import ZipFile, ZIP_STORED, ZIP_DEFLATED, BadZipFile
from lxml import etree

VERSION = 'word-quality-v1'
MAX_INPUT = 16 * 1024**2
NS = {'w':'http://schemas.openxmlformats.org/wordprocessingml/2006/main',
      'wp':'http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing',
      'a':'http://schemas.openxmlformats.org/drawingml/2006/main',
      'm':'http://schemas.openxmlformats.org/officeDocument/2006/math'}
W='{'+NS['w']+'}'


class Rejected(ValueError):
    pass


def parse(raw):
    if b'<!DOCTYPE' in raw.upper() or b'<!ENTITY' in raw.upper():
        raise Rejected('unsafe_xml')
    return etree.fromstring(raw,etree.XMLParser(resolve_entities=False,no_network=True))


def package(source):
    if not 0 < len(source) <= MAX_INPUT:
        raise Rejected('file_limit')
    with ZipFile(BytesIO(source)) as archive:
        infos=archive.infolist()
        names={p.filename for p in infos}
        if len(infos)!=len(names) or len(infos)>2048 or sum(p.file_size for p in infos)>48*1024**2:
            raise Rejected('package_limit')
        if not {'[Content_Types].xml','word/document.xml'} <= names:
            raise Rejected('invalid_docx')
        parts={}
        for p in infos:
            name=p.filename.lower()
            if (p.file_size>MAX_INPUT or p.flag_bits&1 or p.compress_type not in (ZIP_STORED,ZIP_DEFLATED)
                or name.startswith(('/', 'word/embeddings/','word/activex/')) or '\\' in name
                or '..' in PurePosixPath(name).parts or 'vbaproject' in name):
                raise Rejected('unsupported_package_part')
            raw=archive.read(p)
            if name.endswith(('.xml','.rels')):
                tree=parse(raw)
                if name=='[content_types].xml' and not any(
                    n.get('PartName')=='/word/document.xml' and n.get('ContentType')==
                    'application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml' for n in tree):
                    raise Rejected('invalid_docx')
                if name.endswith('.rels') and any(n.get('TargetMode','').lower()=='external'
                    and not n.get('Type','').endswith('/hyperlink') for n in tree):
                    raise Rejected('external_resource')
                if name.startswith('word/'):
                    import re
                    fields=' '.join(' '.join(n.attrib.values())+' '+(n.text or '') for n in tree.iter()
                                    if n.tag in (W+'instrText',W+'fldSimple'))
                    if re.search(r'\b(?:DDE(?:AUTO)?|INCLUDETEXT|INCLUDEPICTURE|LINK|DATABASE)\b',fields,re.I):
                        raise Rejected('external_field')
            parts[p.filename]=raw
        return infos,parts


def number(node,key,default=None):
    try:
        value=int(node.get(W+key)) if node is not None and node.get(W+key) is not None else default
        if value is None or abs(value)>10000000:
            raise ValueError()
        return value
    except (ValueError,TypeError):
        raise Rejected('invalid_layout_measurement') from None


def protected(tree):
    """All text/fields, math and drawing relationships survive every permitted repair."""
    return {
        'text':[(n.tag,n.text or '',tuple(sorted(n.attrib.items()))) for n in tree.iter()
                if n.tag in (W+'t',W+'instrText',W+'delText',W+'tab',W+'br')],
        'math':[etree.tostring(n,method='c14n') for n in tree.xpath('//m:oMath',namespaces=NS)],
        'links':[(n.tag,tuple(sorted(n.attrib.items()))) for n in tree.iter()
                 if isinstance(n.tag,str) and n.tag.endswith(('}blip','}bookmarkStart','}bookmarkEnd','}hyperlink'))],
        'tables':[[len(row.findall(W+'tc')) for row in table.findall(W+'tr')]
                  for table in tree.iter(W+'tbl')],
    }


def inspect(source,repair=False):
    infos,parts=package(source)
    tree=parse(parts['word/document.xml'])
    before=protected(tree)
    body=tree.find(W+'body')
    if body is None:
        raise Rejected('document_body_missing')
    sections=list(tree.iter(W+'sectPr'))
    if not sections:
        raise Rejected('page_geometry_missing')
    geometries=[]
    for section in sections:
        page,margin=section.find(W+'pgSz'),section.find(W+'pgMar')
        width=number(page,'w')-number(margin,'left')-number(margin,'right')-number(margin,'gutter',0)
        height=number(page,'h')-number(margin,'top')-number(margin,'bottom')
        if not 1440<=width<=40000 or not 1440<=height<=40000:
            raise Rejected('invalid_page_geometry')
        columns=section.find(W+'cols')
        if columns is not None and number(columns,'num',1)>1:
            # Column flow has different effective bounds; do not silently flatten it.
            raise Rejected('unsupported_multi_column_layout')
        geometries.append((width,height))
    fixes=[]
    counts={'tables':0,'cells':0,'images':0,'headings':0,'formulas':len(before['math'])}
    warnings=set()
    styles=parse(parts['word/styles.xml']) if 'word/styles.xml' in parts else None
    style_map={} if styles is None else {s.get(W+'styleId'):s for s in styles.findall(W+'style')}
    def paragraph_property(paragraph,prop):
        direct=paragraph.find(W+'pPr/'+W+prop)
        if direct is not None:return direct
        ps=paragraph.find(W+'pPr/'+W+'pStyle')
        key=None if ps is None else ps.get(W+'val')
        for _ in range(12):
            style=style_map.get(key)
            if style is None:return None
            found=style.find(W+'pPr/'+W+prop)
            if found is not None:return found
            base=style.find(W+'basedOn');key=None if base is None else base.get(W+'val')
        return None
    if not any((n.text or '').strip() for n in tree.iter(W+'t')) and not tree.xpath('//m:oMath|//wp:inline|//wp:anchor',namespaces=NS):
        raise Rejected('empty_document')

    def change(code,operation):
        if not repair:
            raise Rejected(code)
        operation()
        fixes.append(code)

    index=0
    for block in body:
        width,height=geometries[min(index,len(geometries)-1)]
        if block.tag==W+'p':
            style=block.find(W+'pPr/'+W+'pStyle')
            outline=paragraph_property(block,'outlineLvl')
            style_id='' if style is None else style.get(W+'val','').lower()
            if style_id.startswith('heading') or style_id=='title' or outline is not None:
                counts['headings']+=1
            if (style_id.startswith('heading') or (outline is not None and number(outline,'val')<9)) and block.getnext() is not None:
                keep=paragraph_property(block,'keepNext')
                if keep is None or keep.get(W+'val','1') in ('0','false','off'):
                    def keep_heading():
                        pp=block.find(W+'pPr')
                        if pp is None:pp=etree.Element(W+'pPr');block.insert(0,pp)
                        node=pp.find(W+'keepNext')
                        if node is None:node=etree.SubElement(pp,W+'keepNext')
                        node.set(W+'val','1')
                    change('heading_may_orphan',keep_heading)
        for table in block.iter(W+'tbl'):
            if table.getparent().tag==W+'tc':
                raise Rejected('unsupported_nested_table')
            counts['tables']+=1
            rows=table.findall(W+'tr')
            counts['cells']+=sum(len(r.findall(W+'tc')) for r in rows)
            grid=table.find(W+'tblGrid')
            columns=[] if grid is None else grid.findall(W+'gridCol')
            if not columns or not 1<=len(columns)<=30:
                raise Rejected('table_grid_missing')
            widths=[number(c,'w') for c in columns]
            if min(widths)<=0:
                raise Rejected('invalid_table_width')
            props=table.find(W+'tblPr')
            indent=0 if props is None else number(props.find(W+'tblInd'),'w',0)
            available=width-max(0,indent)
            total=sum(widths)
            declared=None if props is None else props.find(W+'tblW')
            overflow=(total>available+4 or (declared is not None and declared.get(W+'type')=='dxa'
                      and number(declared,'w',0)>available+4))
            for row in rows:
                if sum(number(c.find(W+'tcPr/'+W+'tcW'),'w',0) for c in row.findall(W+'tc'))>available+4:
                    overflow=True
            if overflow:
                rectangular=all(len(r.findall(W+'tc'))==len(columns) for r in rows)
                if not rectangular or table.xpath('.//w:gridSpan|.//w:vMerge',namespaces=NS):
                    raise Rejected('unsupported_merged_table_overflow')
                minimum=680
                if len(columns)*minimum>available:
                    raise Rejected('table_cannot_fit_readably')
                allocated=[minimum]*len(columns)
                rest=available-sum(allocated)
                weights=[sqrt(max(1,max((len(''.join(c.itertext())) for r in rows
                         for j,c in enumerate(r.findall(W+'tc')) if j==i),default=1))) for i in range(len(columns))]
                for i in range(len(columns)-1):
                    allocated[i]+=round(rest*weights[i]/sum(weights))
                allocated[-1]=available-sum(allocated[:-1])
                def fit_table():
                    nonlocal props
                    if props is None:
                        props=etree.Element(W+'tblPr');table.insert(0,props)
                    for tag,value in [('tblW',str(available)),('tblLayout','fixed')]:
                        node=props.find(W+tag)
                        if node is None:
                            node=etree.SubElement(props,W+tag)
                        node.set(W+('w' if tag=='tblW' else 'type'),value)
                        if tag=='tblW': node.set(W+'type','dxa')
                    for col,size in zip(columns,allocated): col.set(W+'w',str(size))
                    for row in rows:
                        for cell,size in zip(row.findall(W+'tc'),allocated):
                            cp=cell.find(W+'tcPr')
                            if cp is None: cp=etree.Element(W+'tcPr');cell.insert(0,cp)
                            cw=cp.find(W+'tcW')
                            if cw is None: cw=etree.SubElement(cp,W+'tcW')
                            cw.set(W+'w',str(size));cw.set(W+'type','dxa')
                change('table_outside_page',fit_table)
            headers=[i for i,r in enumerate(rows) if r.find(W+'trPr/'+W+'tblHeader') is not None]
            if headers and headers!=list(range(len(headers))):
                raise Rejected('noncontiguous_table_headers')
            if len(rows)>20 and not headers: warnings.add('long_table_header_not_declared')
            for row in rows:
                exact=row.find(W+'trPr/'+W+'trHeight')
                if exact is not None and exact.get(W+'hRule')=='exact':
                    change('exact_table_row_may_clip',lambda:exact.set(W+'hRule','atLeast'))
        for inline in block.xpath('.//wp:inline|.//wp:anchor',namespaces=NS):
            counts['images']+=1
            if inline.tag.endswith('}anchor'): warnings.add('floating_position_not_verified')
            extent=inline.find('{'+NS['wp']+'}extent')
            try: cx,cy=int(extent.get('cx')),int(extent.get('cy'))
            except (AttributeError,TypeError,ValueError): raise Rejected('image_extent_missing') from None
            if cx<=0 or cy<=0: raise Rejected('image_extent_invalid')
            bound_w,bound_h=width*635,height*635
            cell=next((p for p in inline.iterancestors() if p.tag==W+'tc'),None)
            if cell is not None:
                bound_w=min(bound_w,max(1,number(cell.find(W+'tcPr/'+W+'tcW'),'w',width)-216)*635)
            if cx>bound_w or cy>bound_h:
                if inline.tag.endswith('}anchor'): raise Rejected('floating_image_outside_page')
                scale=min(bound_w/cx,bound_h/cy)
                def fit_image():
                    nx,ny=max(1,int(cx*scale)),max(1,int(cy*scale))
                    extent.set('cx',str(nx));extent.set('cy',str(ny))
                    for ex in inline.xpath('.//a:xfrm/a:ext',namespaces=NS):
                        ex.set('cx',str(nx));ex.set('cy',str(ny))
                change('image_outside_page',fit_image)
        if block.tag==W+'p' and block.find(W+'pPr/'+W+'sectPr') is not None:
            index+=1
    # Preserve the complete package's content, equations and non-layout assets.
    if protected(tree)!=before:
        raise Rejected('content_preservation_failed')
    output=source
    if fixes:
        changed=etree.tostring(tree,encoding='UTF-8',xml_declaration=True,standalone=True)
        sink=BytesIO()
        with ZipFile(sink,'w') as archive:
            for entry in infos:
                archive.writestr(entry,changed if entry.filename=='word/document.xml' else parts[entry.filename])
        output=sink.getvalue()
        _,after_parts=package(output)
        if any(after_parts[n]!=raw for n,raw in parts.items() if n!='word/document.xml'):
            raise Rejected('package_preservation_failed')
        inspect(output,False)
    return output,{'version':VERSION,'status':'repaired' if fixes else 'checked',
                   'source_sha256':sha256(source).hexdigest(),'output_sha256':sha256(output).hexdigest(),
                   'repairs':fixes,'warnings':sorted(warnings),'counts':counts,
                   'render_verified':False,'source_facts_verified':False}


def main():
    try:
        import resource
        resource.setrlimit(resource.RLIMIT_AS,(256*1024**2,256*1024**2))
        resource.setrlimit(resource.RLIMIT_CPU,(3,3))
        resource.setrlimit(resource.RLIMIT_NOFILE,(32,32))
    except ImportError: pass # Local Windows fixture checks; production is Linux.
    try:
        source=sys.stdin.buffer.read(MAX_INPUT+1)
        output,report=inspect(source,'--repair' in sys.argv[1:])
        sys.stderr.write(json.dumps(report,separators=(',',':'))+'\n')
        sys.stdout.buffer.write(output)
    except (Rejected,BadZipFile,etree.XMLSyntaxError,KeyError,MemoryError):
        sys.stderr.write(json.dumps({'version':VERSION,'status':'rejected','code':
            str(sys.exc_info()[1]) if isinstance(sys.exc_info()[1],Rejected) else 'invalid_document'})+'\n')
        raise SystemExit(2)


if __name__=='__main__': main()
