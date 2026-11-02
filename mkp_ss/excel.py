"""MKP / Excel: the same workbook as before - sheets Gemini <doc>, Claude <doc>, Claude STD <doc>,
four columns CLUSTER|TYPE|ELEMENT|RESULTS, upserted per provision (copied from the old engine)."""
from __future__ import annotations

import re
from pathlib import Path

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font, PatternFill

from transport import clean


def _four(rows):
	return [{k: clean(r.get(k, '')) for k in ('CLUSTER', 'TYPE', 'ELEMENT', 'RESULTS')} for r in rows]


def append_excel(path: Path, document: dict, by_provider: dict) -> None:
	path=Path(path)
	path.parent.mkdir(parents=True,exist_ok=True)
	short={'CGSTA17':'CGST Act','IGSTA17':'IGST Act','CGSTR17':'CGST Rules'}.get(
		document['code'],document['name'])
	names={'GEMINI':f'Gemini {short}','CLAUDE':f'Claude {short}',
		'STANDARD':f'Claude STD {short}'}
	if path.exists():
		wb=load_workbook(path)
	else:
		wb=Workbook()
		wb.remove(wb.active)
	for provider,name in names.items():
		name=re.sub(r'[\[\]:*?/\\]','-',name)[:31]	# Excel sheet names: no [ ] : * ? / \ and at most 31 characters
		ws=wb[name] if name in wb.sheetnames else wb.create_sheet(name)
		if ws.max_row==1 and ws.cell(1,1).value is None:
			for col,value in enumerate(('CLUSTER','TYPE','ELEMENT','RESULTS'),1):
				ws.cell(1,col,value)
		else:
			head=[ws.cell(1,k).value for k in range(1,5)]
			if head!=['CLUSTER','TYPE','ELEMENT','RESULTS']:
				raise RuntimeError(f'Excel {path.name}/{name} does not have the required four headers')
		incoming=_four(by_provider.get(provider,[]))
		if not incoming:
			continue
		affected={(r['CLUSTER'],r['ELEMENT']) for r in incoming if r['TYPE'] not in {'BATCH','SUM'}}
		clusters={r['CLUSTER'] for r in incoming}
		origin_keys={r['ELEMENT'].split('#',1)[0] for r in incoming if r['TYPE']=='MKP'}
		new_unl={r['ELEMENT'] for r in incoming if r['TYPE']=='UNL'}
		# Upsert all rows for the selected legal provisions; keep other sections untouched.
		for i in range(ws.max_row,1,-1):
			cluster,kind,element=(clean(ws.cell(i,j).value) for j in (1,2,3))
			old_note=clean(ws.cell(i,4).value)
			stale_boundary=(kind=='UNL' and element not in new_unl and cluster in clusters
				and any(f'source of node {key}' in old_note for key in origin_keys))
			if (cluster,element) in affected or (cluster in clusters and kind in {'BATCH','SUM'}) or stale_boundary:
				ws.delete_rows(i)
		for r in incoming:
			ws.append([r[k] for k in ('CLUSTER','TYPE','ELEMENT','RESULTS')])
		ws.freeze_panes='A2'
		ws.auto_filter.ref=ws.dimensions
		for col,width in {'A':19,'B':13,'C':42,'D':100}.items():
			ws.column_dimensions[col].width=width
		for cell in ws[1]:
			cell.fill=PatternFill('solid',fgColor='20324A')
			cell.font=Font(name='Arial',size=11,bold=True,color='FFFFFF')
		for row in ws.iter_rows(min_row=2,min_col=1,max_col=4):
			row[3].alignment=Alignment(vertical='top',wrap_text=True)
	try:
		# Atomic replace protects the existing workbook if saving fails.
		import os,tempfile
		fd,tmp=tempfile.mkstemp(suffix='.xlsx',dir=str(path.parent));os.close(fd)
		try:
			wb.save(tmp)
			os.replace(tmp,path)
		finally:
			if os.path.exists(tmp):os.remove(tmp)
	except PermissionError as exc:
		raise RuntimeError(f'Close {path.name} in Excel before MKP writes it') from exc
