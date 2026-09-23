"""Run on the Hub host; emit a one-time test-site launch only to the QA process."""
import os
from pathlib import Path
import json
import sys
from dotenv import load_dotenv

os.chdir('/opt/kosmos-hub/app/server')
sys.path.insert(0, os.getcwd())
load_dotenv('/etc/kosmos-hub/kosmos-hub.env')
from sqlalchemy import select
from app.db.base import Base
from app.db.session import SessionLocal
from app.models.site import Site
from app.models.hub_user import HubUser
from app.core.security import get_secret_cipher
from app.services.site_admin_launch import SiteAdminLaunchService

with SessionLocal() as db:
    site = db.get(Site, 2)
    assert site.domain == 'test-gasthofloewen.kosmos-medien.de'
    user = db.scalar(select(HubUser).where(HubUser.role == 'admin', HubUser.is_active.is_(True)))
    launch = SiteAdminLaunchService(db=db, cipher=get_secret_cipher()).open_admin(site_id=site.id, actor=user.username, destination='plugins')
    print(json.dumps({'url': launch.launch_url}))
