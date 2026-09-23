"""Проверки отличий источников Systeme Electric от IEK."""
import tempfile
import unittest
from pathlib import Path
import pandas as pd
from openpyxl import Workbook
from prepare_systeme_data import read_constraints, read_snapshot
from prepare_ml_data import read_matrix

class SystemeSourceTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
  self.root=Path(self.temp.name)
 def book(self,name,rows):
  w=Workbook()
  for row in rows:w.active.append(row)
  p=self.root/name;w.save(p);w.close();return p
 def test_pack_size_is_not_minimum_and_unknown_not_one(self):
  p=self.book('MOQ.xlsx',[
   ['Номенклатура.Код','Номенклатура','Артикул','Кратность'],
   ['001_','Товар','A',6],['002_','Другой','B',0],['003_','Третий','C',None]])
  result=read_constraints(p,{},[]).set_index('sku')
  self.assertEqual(result.loc['001_','pack_size'],6)
  self.assertTrue(result.min_order_qty.isna().all())
  self.assertTrue(result.loc[['002_','003_'],'pack_size'].isna().all())
 def test_conflicting_constraints_rejected(self):
  p=self.book('MOQ.xlsx',[
   ['Номенклатура.Код','Номенклатура','Артикул','Кратность'],
   ['001_','Товар','A',6],['001_','Товар','A',12]])
  with self.assertRaises(ValueError):read_constraints(p,{},[])
 def test_snapshot_preserves_zero_unknown_negative_and_eta(self):
  p=self.book('Путь 22.09.2026.xlsx',[
   ['Код 1с','Наименование','Артикул поставщика','Свободный остаток','СЭ в пути 24.09'],
   ['001_','Товар','A',0,12],['002_','Другой','B',None,0],['003_','Третий','C',-5,None]])
  stock,transit=read_snapshot(p,{},[])
  self.assertEqual(stock.free_stock_raw.iloc[0],0)
  self.assertTrue(pd.isna(stock.free_stock_raw.iloc[1]))
  self.assertEqual(stock.free_stock_raw.iloc[2],-5)
  self.assertEqual(transit.qty_raw.tolist(),[12,0])
  self.assertTrue(transit.eta.eq(pd.Timestamp('2026-09-24')).all())
  self.assertTrue(transit.eta_year_inferred.all())
  self.assertFalse(stock.warehouse_confirmed.any())
 def test_monthly_unit_alias_and_codes_preserved(self):
  p=self.book('Остатки.xlsx',[
   ['Номенклатура','Номенклатура.Код','Ед.изм','янв. 2024'],
   ['Товар','001_','шт',12]])
  catalog={};result=read_matrix(p,'stock',catalog,[])
  self.assertEqual(catalog['001_']['unit'],'шт')
  self.assertEqual(result.stock_qty_raw.iloc[0],12)

if __name__=='__main__':unittest.main()
