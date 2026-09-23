#!/usr/bin/env python3
"""Обучение Systeme Electric общими алгоритмами, без параметров заказа IEK."""
import argparse
import hashlib
import json
import pickle
from pathlib import Path
import numpy as np
import pandas as pd
from train_iek_model import (ROOT, CANDIDATES, read_csv, attach_history, validate_inputs,
    rolling_backtest, choose_model, metric_table, fit_model, predict_model)


def run(input_dir,output_dir,model_dir,max_iter=160):
 report=json.loads((input_dir/'preparation_report.json').read_text())
 if report.get('supplier_id')!='SYSTEME': raise ValueError('Ожидаются данные SYSTEME')
 if max_iter<1: raise ValueError('Число итераций должно быть положительным')
 if output_dir.resolve()==(ROOT/'data/forecasts/iek').resolve() or model_dir.resolve()==(ROOT/'models/iek').resolve():
  raise ValueError('Результаты IEK нельзя перезаписывать')
 for source in report['sources'].values():
  if hashlib.sha256(Path(source['path']).read_bytes()).hexdigest()!=source['sha256']:
   raise ValueError('Исходный файл изменился: повторите подготовку')
 data=read_csv(input_dir/'training_data.csv',['target_month'])
 prediction=read_csv(input_dir/'prediction_features.csv',['target_month'])
 panel=read_csv(input_dir/'monthly_panel.csv',['month'])
 validate_inputs(data,prediction,panel,report)
 data=attach_history(data,panel);prediction=attach_history(prediction,panel)
 validation=rolling_backtest(data,'validation',max_iter)
 winner,scores=choose_model(validation)
 print(f'Выбран на validation: {winner}',flush=True)
 test=rolling_backtest(data,'test',max_iter)
 backtest=pd.concat([validation,test],ignore_index=True)
 metrics=metric_table(backtest)
 diagnostics=[]
 for name,group in test.groupby('model'):
  ape=(group.predicted_qty-group.actual_qty).abs()/group.actual_qty.replace(0,np.nan)
  diagnostics.append(dict(model=name,rows=len(group),positive_actual_rows=int(group.actual_qty.gt(0).sum()),
   median_ape=float(ape.median()),share_within_20pct=float(ape.dropna().le(.2).mean())))
 models={name:fit_model(name,data,max_iter) for name in CANDIDATES}
 for model in models.values():
  model.update(supplier_id='SYSTEME',trained_through=str(data.target_month.max().date()),
   prediction_month=report['prediction_month'],minimum_history=report['policies']['min_history'])
 selected=models[winner]
 sufficient=prediction.history_observed_months.ge(report['policies']['min_history'])
 forecast=prediction[['sku','unit','target_month']].copy()
 forecast['predicted_qty']=np.nan
 forecast.loc[sufficient,'predicted_qty']=predict_model(selected,prediction.loc[sufficient])
 forecast['status']=np.where(sufficient,'forecast','insufficient_history')
 forecast['supplier_id']='SYSTEME';forecast['model']=winner
 coverage=[]
 for month in sorted(data.loc[data.split.ne('train'),'target_month'].unique()):
  actual=panel[panel.month.eq(month)]
  coverage.append(dict(month=str(pd.Timestamp(month).date()),catalog_rows=len(actual),
   known_nonnegative_targets=int(actual.target_qty.notna().sum()),evaluated_rows=int(data.target_month.eq(month).sum())))
 summary=dict(supplier_id='SYSTEME',as_of=report['as_of'],winner=winner,validation_scores=scores,
  selection_metric='mean validation WAPE across known units; equal unit weights',splits=report['splits'],
  coverage=coverage,training_rows=len(data),forecast_products=len(forecast),products_with_forecast=int(sufficient.sum()),
  max_iter=max_iter,test_diagnostics=diagnostics,stock_features_enabled=False,prepared_file_hashes={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in input_dir.glob('*.csv')},
  warnings=report['warnings']+['Тест помесячный: перед прогнозом августа известен июль.',
   'Метрики относятся только к известным неотрицательным продажам с достаточной историей.',
   'Сентябрь прогнозируется целиком по истории до августа. Это не остаток спроса с 23 сентября.',
   'Нет расчёта 35-дневного горизонта, страхового запаса и заказа. Параметры IEK не использованы.',
   'Модель не подключена к backend.'])
 output_dir.mkdir(parents=True,exist_ok=True);model_dir.mkdir(parents=True,exist_ok=True)
 for name,frame in [('backtest_predictions',backtest),('metrics_by_unit',metrics),('monthly_forecast',forecast)]:
  frame.to_csv(output_dir/f'{name}.csv',index=False,encoding='utf-8-sig',na_rep='')
 for name,model in {**models,'selected_model':selected}.items():
  with (model_dir/f'{name}.pkl').open('wb') as f: pickle.dump(model,f,protocol=pickle.HIGHEST_PROTOCOL)
 (output_dir/'training_report.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2,allow_nan=False),encoding='utf-8')
 lines=['# Результаты обучения Systeme Electric','',f'Выбран по validation: `{winner}`.','',
  '| Период | Вариант | Ед. | Строк | WAPE | MAE | Bias |',
  '|---|---|---|---:|---:|---:|---:|']
 for row in metrics.itertuples():
  lines.append(f'| {row.split} | {row.model} | {row.unit} | {row.rows} | {row.wape:.2%} | {row.mae:.2f} | {row.bias:.2%} |')
 chosen=next(d for d in diagnostics if d['model']==winner)
 lines+=['',f"На тесте медианная ошибка выбранного варианта {chosen['median_ape']:.2%}; доля прогнозов с ошибкой не более 20% — {chosen['share_within_20pct']:.2%}."]
 lines+=['',f'Прогноз для {int(sufficient.sum())} из {len(forecast)} товаров.','',
  'WAPE — сумма абсолютных ошибок / сумма фактов в одной единице. Это не процент точности.',
  '', '## Ограничения','']+['- '+s for s in summary['warnings']]
 (output_dir/'training_summary.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
 return summary

if __name__=='__main__':
 p=argparse.ArgumentParser(description=__doc__)
 p.add_argument('--input-dir',type=Path,default=ROOT/'data/ml/systeme')
 p.add_argument('--output-dir',type=Path,default=ROOT/'data/forecasts/systeme')
 p.add_argument('--model-dir',type=Path,default=ROOT/'models/systeme')
 p.add_argument('--max-iter',type=int,default=160)
 a=p.parse_args();r=run(a.input_dir,a.output_dir,a.model_dir,a.max_iter)
 print(json.dumps({k:r[k] for k in ('winner','validation_scores','products_with_forecast')},ensure_ascii=False,indent=2))
