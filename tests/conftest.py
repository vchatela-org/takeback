import os
import tempfile

# Config is read when app.database is first imported, by whichever test
# module gets there first: point the app at a throwaway database and data
# dir before any of them is collected, so no test ever touches ./data.
_TMP = tempfile.mkdtemp(prefix="tb-test-")
os.environ.setdefault("TAKEBACK_DATABASE_URL", f"sqlite:///{_TMP}/test.db")
os.environ.setdefault("TAKEBACK_DATA_DIR", _TMP)
# Never a real InfluxDB, whatever the shell or .env says: the app would start
# its scheduler at import and push the synthetic test data there.
os.environ["TAKEBACK_INFLUXDB_URL"] = ""
