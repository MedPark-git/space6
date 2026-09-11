"""Presentation-only normalization; saved historical records are not rewritten."""
import re

FORMAT_VERSION = '2026-09-11-a3-master-a4-fit-v5'
CIRCLES = '①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳'
LETTERS = 'ㄱㄴㄷㄹㅁㅂㅅㅇㅈㅊㅋㅌㅍㅎ'


def circle(index):
    if index <= 20:
        return CIRCLES[index-1]
    if index <= 35:
        return chr(0x3251+index-21)
    if index <= 50:
        return chr(0x32B1+index-36)
    return f'({index})'


def letter(index):
    value=''
    while index:
        index, digit = divmod(index-1,len(LETTERS))
        value=LETTERS[digit]+value
    return value+'.'


def single_line(value):
    """Keep every supplied word; concise summarization is the AI prompt's job."""
    return re.sub(r'\s+', ' ',str(value)).strip()


def executive_honorific(value):
    """Use MedPark's required honorific without producing '대표이사님님'."""
    return re.sub(r'대표이사(?!님)', '대표이사님', str(value))


def format_discussion(value):
    """Remove the summary report and normalize new/legacy heading markers.

    Only line-leading list markers change; dates, amounts and numbered text
    inside sentences are retained. Explicit summary-report sections are omitted.
    """
    raw=executive_honorific(value).replace('\r\n','\n').replace('\r','\n').split('\n')
    output=[]
    top=second=third=fourth=0
    section=''
    skip_section=False
    legacy_group=False
    circle_pattern=r'[①-⑳㉑-㉟㊱-㊿]'
    for original in raw:
        text=original.strip()
        text=re.sub(r'^#{1,6}\s+','',text)
        unbold=text[2:-2] if text.startswith('**') and text.endswith('**') else text
        heading=re.match(r'^(\d{1,2})\.\s+(.+)$',unbold)
        title=heading.group(2).strip() if heading else unbold
        if re.fullmatch(r'(?:요약\s*보고서|회의\s*정보)\s*:?',title):
            skip_section=True
            continue
        if heading:
            skip_section=False
            top+=1;second=third=fourth=0;legacy_group=False
            section=title
            output.append(f'{top}. {title}')
            continue
        if skip_section:
            continue
        if not text:
            if output and output[-1]!='':output.append('')
            continue
        compound=re.match(r'^\d+-\d+\)\s*(.*)$',unbold)
        sub=re.match(r'^\d+\)\s*(.*)$',unbold)
        circled=re.match(r'^'+circle_pattern+r'\s*(.*)$',unbold)
        alphabet=re.match(r'^[ㄱ-ㅎ]+\.\s*(.*)$',unbold)
        bullet=re.match(r'^[-•]\s+(.*)$',text)
        if sub or (compound and ('의사결정' in section or not second)):
            second+=1;third=fourth=0;legacy_group=False
            output.append(f'  {second}) '+(sub or compound).group(1))
        elif compound:
            third+=1;fourth=0;legacy_group=True
            output.append(f'    {circle(third)} '+compound.group(1))
        elif circled:
            if legacy_group:
                fourth+=1;output.append(f'      {letter(fourth)} '+circled.group(1))
            else:
                third+=1;fourth=0;output.append(f'    {circle(third)} '+circled.group(1))
        elif alphabet:
            fourth+=1;output.append(f'      {letter(fourth)} '+alphabet.group(1))
        elif text.startswith('담당자:') and '후속' in section:
            second+=1;third=fourth=0;legacy_group=False
            output.append(f'  {second}) '+text)
        elif bullet:
            if second and '후속' in section:
                if bullet.group(1).startswith('세부 내용:'):
                    fourth+=1;output.append(f'      {letter(fourth)} '+bullet.group(1))
                else:
                    third+=1;fourth=0;output.append(f'    {circle(third)} '+bullet.group(1))
            elif second and '회의 정보' not in section:
                third+=1;fourth=0;output.append(f'    {circle(third)} '+bullet.group(1))
            else:
                second+=1;third=fourth=0;output.append(f'  {second}) '+bullet.group(1))
        else:
            output.append(original.rstrip())
    return '\n'.join(output).strip()


def presentation(data):
    normalized={key:(executive_honorific(value) if isinstance(value,str) else
        [executive_honorific(item) if isinstance(item,str) else item for item in value]
        if isinstance(value,list) else value) for key,value in data.items()}
    return dict(normalized, discussion=format_discussion(normalized.get('discussion','')),
        conclusions=[single_line(executive_honorific(item))
                     for item in normalized.get('conclusions',[])])
