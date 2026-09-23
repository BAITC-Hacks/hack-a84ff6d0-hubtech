#!/usr/bin/env python3
"""Подготовка шести файлов Systeme Electric без изменения Excel."""
import argparse
import hashlib
import json
import re
from collections import Counter
from pathlib import Path
import numpy as np
import pandas as pd
from prepare_ml_data import (ROOT, FEATURES, ISSUE_COLUMNS, table, value, code, clean_text,
    catalog_add, quantity, issue, read_matrix, read_transactions, read_seasonality, build_features, reconcile)

FILES = {
 'sales':'Ежемесячные продажи в кол-м выражении SystemElectric 2024-2026.xlsx',
 'stock':'Ежемесячные остатки SystemElectric 2024-2026.xlsx',
 'transactions':'Динамика продаж_Syseme Electric_2025-2026.xlsx',
 'moq':'MOQ SystemElectric.xlsx',
 'transit':'Товар в пути_SystemElectric на 22.09.2026.xlsx',
 'seasonality':'Сезонность SystemElectric 2024-2026.xlsx',
}

def read_constraints(path,catalog,issues):
 records={}
 with table(path,'Номенклатура.Код') as (headers,rows):
  ci,ni,ai,qi=[headers.index(s) for s in ('номенклатура.код','номенклатура','артикул','кратность')]
  for rn,row in rows:
   if clean_text(value(row,ni)).casefold() in ('итого','всего'): continue
   sku=code(value(row,ci))
   if not sku: continue
   article=clean_text(value(row,ai)); catalog_add(catalog,sku,value(row,ni),supplier_sku=article)
   qty,status=quantity(value(row,qi),issues,'pack_size',rn,headers[qi],sku)
   pack=qty if np.isfinite(qty) and qty>0 and float(qty).is_integer() else np.nan
   if pd.isna(pack): issue(issues,'pack_size',rn,headers[qi],sku,'unknown_pack_size',value(row,qi))
   record=dict(sku=sku,supplier_sku=article,pack_size=pack,min_order_qty=np.nan)
   if sku in records:
    old=records[sku]
    if old['supplier_sku']!=article or not (old['pack_size']==pack or pd.isna(old['pack_size']) and pd.isna(pack)):
     raise ValueError(f'Противоречивая кратность {sku}')
    issue(issues,'pack_size',rn,headers[ci],sku,'duplicate_constraint_removed',sku)
   else: records[sku]=record
 return pd.DataFrame(records.values())

def read_snapshot(path,catalog,issues):
 match=re.search(r'\d{2}\.\d{2}\.\d{4}',path.name)
 snapshot=pd.to_datetime(match.group(),format='%d.%m.%Y')
 stocks=[]; arrivals=[]; seen=set()
 with table(path,'Код 1с') as (headers,rows):
  ci,ni,ai,si=[headers.index(s) for s in ('код 1с','наименование','артикул поставщика','свободный остаток')]
  arrival_cols={}
  for i,h in enumerate(headers):
   m=re.search(r'в пути\s+(\d{2})\.(\d{2})(?:\.(\d{4}))?',h)
   if m: arrival_cols[i]=pd.Timestamp(year=int(m[3] or snapshot.year),month=int(m[2]),day=int(m[1]))
  if not arrival_cols: raise ValueError('Нет колонок поставок')
  for rn,row in rows:
   if clean_text(value(row,ni)).casefold() in ('итого','всего'): continue
   sku=code(value(row,ci))
   if not sku: continue
   if sku in seen: raise ValueError(f'Повтор товара в снимке: {sku}')
   seen.add(sku); catalog_add(catalog,sku,value(row,ni),supplier_sku=value(row,ai))
   qty,status=quantity(value(row,si),issues,'free_stock',rn,headers[si],sku)
   stocks.append(dict(sku=sku,source_as_of=snapshot,free_stock_raw=qty,quantity_status=status,
     warehouse='',warehouse_confirmed=False,unit_conversion_confirmed=False))
   for col,eta in arrival_cols.items():
    raw=value(row,col)
    if raw is None or clean_text(raw)=='': continue
    q,st=quantity(raw,issues,'transit',rn,headers[col],sku)
    arrivals.append(dict(sku=sku,shipment=headers[col],eta=eta,qty_raw=q,quantity_status=st,
      source_as_of=snapshot,eta_year_inferred=True,unit_conversion_confirmed=False))
 return pd.DataFrame(stocks),pd.DataFrame(arrivals,columns=['sku','shipment','eta','qty_raw','quantity_status','source_as_of','eta_year_inferred','unit_conversion_confirmed'])

def prepare(input_dir,output_dir,as_of):
 paths={k:input_dir/v for k,v in FILES.items()};catalog={};issues=[]
 stock=read_matrix(paths['stock'],'stock',catalog,issues)
 sales=read_matrix(paths['sales'],'sales',catalog,issues)
 transactions=read_transactions(paths['transactions'],catalog,issues)
 constraints=read_constraints(paths['moq'],catalog,issues)
 free_stock,transit=read_snapshot(paths['transit'],catalog,issues)
 seasonality=read_seasonality(paths['seasonality'],issues)
 products=pd.DataFrame(catalog.values()).sort_values('sku').reset_index(drop=True)
 cutoff=min(pd.Timestamp(as_of).normalize(),transactions.date.max().normalize()+pd.Timedelta(days=1))
 # Тип месячного снимка не подтверждён. Сохраняем источник, но не используем остатки в ML.
 feature_stock=stock.copy();feature_stock['stock_qty_raw']=np.nan
 panel,train,prediction=build_features(sales,feature_stock,products,cutoff)
 panel=panel.drop(columns=['stock_qty_raw','stock_status']).merge(stock,on=['sku','month'],how='left',validate='one_to_one')
 comparison=reconcile(sales,transactions,cutoff)
 report=dict(supplier_id='SYSTEME',as_of=pd.Timestamp(as_of).date().isoformat(),
  data_cutoff_exclusive=cutoff.date().isoformat(),prediction_month=str(prediction.target_month.iloc[0].date()),
  policies=dict(blank_sales='missing',negative_sales='missing',min_history=3),feature_columns=FEATURES,
  target_column='target_qty',time_column='target_month',group_column='sku',
  splits={name:dict(rows=len(g),start=str(g.target_month.min().date()),end=str(g.target_month.max().date())) for name,g in train.groupby('split')},
  counts=dict(products=len(products),monthly_rows=len(panel),training_rows=len(train),transactions=len(transactions),
   constraints=len(constraints),transit_rows=len(transit),positive_transit_rows=int(transit.qty_raw.gt(0).sum()),
   stock_snapshot_rows=len(free_stock),prediction_with_sufficient_history=int(prediction.history_sufficient.sum()),
   comparable_sku_months=int(comparison.comparable.sum()),mismatched_sku_months=int((comparison.comparable & ~comparison.matches_signed).sum())),
  issues=dict(Counter(r['issue'] for r in issues)),
  warnings=['Кратность не является отдельным MOQ. Неизвестный минимум не заменяется единицей.',
   'Пустые и отрицательные продажи сохранены; в цели используются только известные неотрицательные значения.',
   'Месячные остатки сохранены, но исключены из признаков до уточнения момента снимка.',
   'Год ETA без года взят из даты файла и явно помечен как предположение.',
   'Склад и пересчёт единиц свободного остатка/поставок не подтверждены.',
   'Готовая сезонность не включена в исторические признаки.',
   'Срок поставки, период проверки и сервис Systeme не подтверждены; параметры IEK не перенесены.'],
  sources={k:dict(path=str(p.resolve()),sha256=hashlib.sha256(p.read_bytes()).hexdigest()) for k,p in paths.items()})
 output_dir.mkdir(parents=True,exist_ok=True)
 for name,frame in dict(products=products,monthly_panel=panel,monthly_stock=stock,transactions=transactions,
  purchase_constraints=constraints,free_stock=free_stock,in_transit=transit,provided_seasonality=seasonality,
  reconciliation=comparison,training_data=train,prediction_features=prediction,
  quality_issues=pd.DataFrame(issues,columns=ISSUE_COLUMNS)).items():
  frame.to_csv(output_dir/f'{name}.csv',index=False,encoding='utf-8-sig',na_rep='')
 (output_dir/'preparation_report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
 return report

if __name__=='__main__':
 p=argparse.ArgumentParser(description=__doc__)
 p.add_argument('--input-dir',type=Path,default=ROOT/'Systeme electric')
 p.add_argument('--output-dir',type=Path,default=ROOT/'data/ml/systeme')
 p.add_argument('--as-of',default='2026-09-23')
 a=p.parse_args();print(json.dumps(prepare(a.input_dir,a.output_dir,a.as_of)['counts'],ensure_ascii=False,indent=2))
