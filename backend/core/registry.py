"""What the dashboard can trigger: collectors (no browser), skills (browser), pipelines (LLM, no browser), services."""
from __future__ import annotations
from backend.core.collectors import public_apis, ats_boards, more_collectors, reddit as reddit_collector, hiringcafe as hiringcafe_collector, relocateme as relocateme_collector
from backend.core.skills import linkedin, linkedin_people, jobright, x_outreach, instahyre, cutshort, hirist, ycombinator, wellfound, naukri, peerlist
from backend.core.apply import worker as ats_worker
from backend.core import pipeline, outreach, sheets, gmail

COLLECTORS = {**public_apis.REGISTRY, **ats_boards.REGISTRY, **more_collectors.REGISTRY, 
              **reddit_collector.REGISTRY, **hiringcafe_collector.REGISTRY, **relocateme_collector.REGISTRY}
SKILLS = {**linkedin.SKILLS, **linkedin_people.SKILLS, **jobright.SKILLS, **x_outreach.SKILLS, **ats_worker.SKILLS, **instahyre.SKILLS, **cutshort.SKILLS, **hirist.SKILLS, **ycombinator.SKILLS, **wellfound.SKILLS, **naukri.SKILLS, **peerlist.SKILLS}
PIPELINES = {**pipeline.PIPELINES, **outreach.PIPELINES}
SERVICES = {"sheets_sync": sheets.sync, **gmail.SERVICES}

_C = lambda k, n: {"key": k, "name": n, "type": "collector", "status": "ready", "login": False}
_S = lambda k, n, modes=("discover", "apply"): {"key": k, "name": n, "type": "skill", "status": "ready", "login": True, "modes": list(modes)}
PLATFORMS = [
    _C("weworkremotely", "We Work Remotely"), _C("remoteok", "Remote OK"), _C("remotive", "Remotive"), _C("himalayas", "Himalayas"), _C("arbeitnow", "Arbeitnow"),
    _C("hackernews", "HN Who is hiring"), _C("greenhouse", "Greenhouse boards"), _C("lever", "Lever boards"), _C("ashby", "Ashby boards"),
    _C("japandev", "Japan Dev"),
    _C("hiringcafe", "HiringCafe"),
    _C("relocateme", "Relocate.me (relocation/visa jobs)"),
    _C("reddit", "Reddit hiring posts"),
    {"key": "ats_apply", "name": "ATS apply worker (Greenhouse/Lever/Ashby/Workable)", "type": "skill", "status": "ready", "login": False, "modes": ["run"]},
    _S("linkedin", "LinkedIn", ("discover", "hydrate", "easy_apply", "find_people", "connect", "dm")),
    _S("linkedin_people", "LinkedIn people (find -> connect -> message)", ("find", "connect", "message")),
    _S("ycombinator", "YC Work at a Startup"), _S("wellfound", "Wellfound"), _S("instahyre", "Instahyre"), _S("cutshort", "Cutshort"),
    _S("jobright", "Jobright (AI match feed -> employer ATS)", ("discover",)),
    _S("hirist", "Hirist"), _S("naukri", "Naukri"), _S("peerlist", "Peerlist"),
    _S("x_outreach", "X / Twitter (find -> dm -> reply)", ("find", "dm", "reply")),
    {"key": "gmail", "name": "Gmail outreach", "type": "service", "status": "ready", "login": True},
    {"key": "sheets", "name": "Google Sheet sync", "type": "service", "status": "ready", "login": True},
    {"key": "talclub", "name": "tal.club (mobile)", "type": "skill", "status": "planned", "login": True},
    {"key": "hirect", "name": "Hirect (mobile)", "type": "skill", "status": "planned", "login": True},
]
