import json
import tempfile
import unittest
from unittest.mock import patch
from backend.common import ROOT, canonical, read_json
from backend.database import Database
from backend.observation_query import catalog
from backend.stations import gcj02_to_wgs84, wgs84_to_gcj02, sync_locations, sync_datums


class StationLocationTests(unittest.TestCase):
    def test_inverse_reference_and_all_four_roundtrips(self):
        lon,lat=gcj02_to_wgs84(116.404,39.915)
        self.assertAlmostEqual(lon,116.3977555,places=6)
        self.assertAlmostEqual(lat,39.9135962,places=6)
        self.assertEqual(gcj02_to_wgs84(0.,0.),(0.,0.))
        registry=read_json(ROOT/'config/station-locations.json')
        for item in registry['stations']:
            x,y=gcj02_to_wgs84(item['lon'],item['lat'])
            self.assertTrue(88<x<90 and 31<y<33)
            self.assertGreater(abs(x-item['lon'])+abs(y-item['lat']),.001)
            gx,gy=wgs84_to_gcj02(x,y)
            self.assertAlmostEqual(gx,item['lon'],places=9)
            self.assertAlmostEqual(gy,item['lat'],places=9)
        with self.assertRaises(ValueError):gcj02_to_wgs84(float('nan'),32.)

    def test_existing_station_migration_is_idempotent_and_keeps_data(self):
        with tempfile.TemporaryDirectory() as temp:
            db=Database(temp)
            registry=read_json(ROOT/'config/station-locations.json')
            with db.connect() as conn:
                for item in registry['stations']:
                    conn.execute('INSERT INTO stations VALUES(?,?,?,?,?,?)',(item['id'],item['id'],'lake',None,None,canonical({'datumVerified':False,'notes':'保留原档案'})))
                conn.execute("INSERT INTO sources VALUES('S','test.xlsx','frozen-sha',0,'2025-01-01','{}')")
                conn.execute("INSERT INTO observations(source_id,station_id,variable,time,step,unit,value,raw_text,quality,sheet,cell) VALUES('S','SLC-N','level','2025-01-01T00:00:00','day','m',4544.8,'4544.800','valid','北','A1')")
            self.assertFalse(next(s for s in catalog(db)['stations'] if s['id']=='SLC-N')['located'])
            before=db.rows('SELECT * FROM observations')
            sources=db.rows('SELECT * FROM sources')
            changes=sync_locations(db)
            self.assertEqual(len(changes),4)
            self.assertEqual(db.rows('SELECT * FROM observations'),before)
            self.assertEqual(db.rows('SELECT * FROM sources'),sources)
            self.assertTrue(next(s for s in catalog(db)['stations'] if s['id']=='SLC-N')['located'])
            for row in db.rows('SELECT * FROM stations'):
                meta=json.loads(row['metadata']);original=next(s for s in registry['stations'] if s['id']==row['id'])
                self.assertEqual(meta['notes'],'保留原档案')
                self.assertFalse(meta['datumVerified'])
                self.assertFalse(meta['location']['surveyVerified'])
                self.assertEqual(meta['location']['kCode'],original['kCode'])
                self.assertEqual(meta['location']['sourceLon'],original['lon'])
            self.assertEqual(sync_locations(db),[])
            Database(temp)
            self.assertEqual(len(db.rows("SELECT * FROM audit WHERE action='stations.locations'")),1)

    def test_bad_registry_rolls_back_and_empty_workspace_stays_empty(self):
        with tempfile.TemporaryDirectory() as temp:
            db=Database(temp)
            self.assertEqual(db.rows('SELECT * FROM stations'),[])
            with db.connect() as conn:
                conn.execute("INSERT INTO stations VALUES('SLC-N','北岸','lake',NULL,NULL,'{}')")
                conn.execute("INSERT INTO stations VALUES('SLC-E','东岸','lake',NULL,NULL,'{}')")
            value=read_json(ROOT/'config/station-locations.json')
            value['stations'][1]['lat']=999
            with patch('backend.stations.read_json',return_value=value):
                with self.assertRaises(ValueError):sync_locations(db)
            self.assertTrue(all(s['lon'] is None for s in db.rows('SELECT * FROM stations')))
            self.assertEqual(db.rows('SELECT * FROM audit'),[])

    def test_user_datum_retraction_keeps_confirmation_history_and_raw_values(self):
        with tempfile.TemporaryDirectory() as temp:
            db=Database(temp)
            with db.connect() as conn:
                for identifier in ['SLC','SLC-N','SLC-E','SLC-S','SLC-W']:
                    conn.execute('INSERT INTO stations VALUES(?,?,?,?,?,?)',(identifier,identifier,'lake',89.,32.,canonical({'datumVerified':False,'location':{'surveyVerified':False},'notes':'保留'})))
                conn.execute("INSERT INTO sources VALUES('S','test.xlsx','sha',0,'2025-01-01','{}')")
                conn.execute("INSERT INTO observations(source_id,station_id,variable,time,step,unit,value,raw_text,quality,sheet,cell) VALUES('S','SLC-N','level','2025-01-01T00:00:00','day','m',4544.8,'4544.800','valid','北','A1')")
            before=db.rows('SELECT * FROM observations')
            previous=read_json(ROOT/'config/station-datums.json')
            previous.update(revision='original-user-confirmation',status='user-confirmed',commonDatumConfirmed=True,zeroUnified=True)
            with patch('backend.stations.read_json',return_value=previous):
                self.assertEqual(len(sync_datums(db)),5)
            self.assertEqual(len(sync_datums(db)),5)
            for station in db.rows('SELECT * FROM stations'):
                meta=json.loads(station['metadata'])
                self.assertFalse(meta['datumVerified'])
                self.assertFalse(meta['verticalDatum']['zeroUnified'])
                self.assertEqual(meta['verticalDatum']['referenceStation'],'SLC')
                self.assertIsNone(meta['verticalDatum']['namedDatum'])
                self.assertEqual(meta['verticalDatum']['status'],'user-retracted')
                self.assertFalse(meta['location']['surveyVerified'])
                self.assertEqual(meta['notes'],'保留')
                self.assertEqual((station['lon'],station['lat']),(89.,32.))
            self.assertEqual(before,db.rows('SELECT * FROM observations'))
            self.assertEqual(sync_datums(db),[])
            audits=db.rows("SELECT * FROM audit WHERE action='stations.datums' ORDER BY id")
            self.assertEqual(len(audits),2)
            self.assertTrue(json.loads(audits[-1]['payload'])['changes'][0]['previous']['datumVerified'])


if __name__=='__main__':unittest.main()
