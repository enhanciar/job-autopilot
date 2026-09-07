import os, tempfile, pathlib, shutil
os.environ['AUTOPILOT_DATA_DIR'] = tempfile.mkdtemp(prefix='autopilot-tests-')
root = pathlib.Path(__file__).resolve().parents[1]
for name in ('master_profile.yaml', 'answers.yaml'):
    src = root / 'data' / name
    if not src.exists(): src = root / 'data' / name.replace('.yaml', '.example.yaml')   # fresh clone: use the shipped examples
    shutil.copy(src, pathlib.Path(os.environ['AUTOPILOT_DATA_DIR']) / name)
import pytest
from backend.app.db import Base, engine, init_db

@pytest.fixture(autouse=True)
def database():
    Base.metadata.drop_all(engine)
    init_db()
    yield
