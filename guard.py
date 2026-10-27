"""No-AI lineup safety net: bench any unlocked starter who is OUT/IR/suspended or on bye.

Fills the slot from the bench, directly or by sliding a starter over (e.g. FLEX WR -> WR,
bench RB -> FLEX). Never adds or drops players. Exit 3 = a broken slot it could not fill.
"""
import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

LEAGUE = 'https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/seasons/2026/segments/0/leagues/1215070607'
WRITE = 'https://lm-api-writes.fantasy.espn.com/apis/v3/games/ffl/seasons/2026/segments/0/leagues/1215070607/transactions/'
SCHEDULE = 'https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/seasons/2026?view=proTeamSchedules_wl'
MEMBER = '{B715ECB6-6C3F-4B45-BF20-00856A72A082}'
BAD = {'OUT', 'INJURY_RESERVE', 'SUSPENSION'}
BENCH, IR = 20, 21
# ET deadlines: just after inactives, before kickoff. GitHub fires cron runs hours late or not
# at all, so runs start early and sleep until the next one. Keys are weekday() (Mon=0).
DEADLINES = {6: ['09:05', '11:50', '15:15', '19:10'], 0: ['19:02'], 3: ['19:05']}
SLOT = {0: 'QB', 2: 'RB', 4: 'WR', 6: 'TE', 16: 'D/ST', 17: 'K', 20: 'BN', 23: 'FLEX'}


def cookie():
    return {'Cookie': f"espn_s2={os.environ['ESPN_S2']}; SWID={os.environ['ESPN_SWID']}"}


def get(url):
    return json.load(urllib.request.urlopen(urllib.request.Request(url, headers=cookie()), timeout=30))


def load():
    wk = get(LEAGUE + '?view=mSettings')['status']['latestScoringPeriod']
    kick = {}
    for t in get(SCHEDULE)['settings']['proTeams']:
        g = t.get('proGamesByScoringPeriod', {}).get(str(wk))
        kick[t['id']] = g[0]['date'] / 1000 if g else None
    team = next(t for t in get(LEAGUE + f'?view=mRoster&scoringPeriodId={wk}')['teams'] if t['id'] == 7)
    now = time.time()
    roster = []
    for e in team['roster']['entries']:
        p = e['playerPoolEntry']['player']
        k = kick.get(p['proTeamId'])
        roster.append({
            'id': p['id'], 'name': p['fullName'], 'slot': e['lineupSlotId'], 'elig': set(p['eligibleSlots']),
            'locked': k is not None and k <= now,
            'dead': k is None or p.get('injuryStatus') in BAD,
            'proj': next((s['appliedTotal'] for s in p.get('stats', [])
                          if s['statSourceId'] == 1 and s['scoringPeriodId'] == wk), 0),
        })
    return wk, roster


def fix(b, roster):
    """Best lineup move that takes broken starter b out of its slot, or None."""
    ok = lambda p: not p['dead'] and not p['locked']
    options = []
    for c in (p for p in roster if p['slot'] == BENCH and ok(p) and p['proj'] > 0):
        if b['slot'] in c['elig']:
            options.append((c['proj'], [(c, BENCH, b['slot']), (b, b['slot'], BENCH)]))
        for x in roster:
            if x is not b and x['slot'] not in (BENCH, IR) and ok(x) and b['slot'] in x['elig'] and x['slot'] in c['elig']:
                options.append((c['proj'], [(x, x['slot'], b['slot']), (c, BENCH, x['slot']), (b, b['slot'], BENCH)]))
    return max(options, key=lambda o: o[0])[1] if options else None


def write(wk, moves):
    body = {'isLeagueManager': False, 'teamId': 7, 'type': 'ROSTER', 'memberId': MEMBER,
            'scoringPeriodId': wk, 'executionType': 'EXECUTE',
            'items': [{'playerId': p['id'], 'type': 'LINEUP', 'fromLineupSlotId': a, 'toLineupSlotId': z}
                      for p, a, z in moves]}
    req = urllib.request.Request(WRITE, data=json.dumps(body).encode(),
                                 headers={**cookie(), 'Content-Type': 'application/json'})
    urllib.request.urlopen(req, timeout=30)


def wait():
    now = datetime.now(ZoneInfo('America/New_York'))
    for hm in DEADLINES.get(now.weekday(), []):
        t = now.replace(hour=int(hm[:2]), minute=int(hm[3:]), second=0, microsecond=0)
        if t + timedelta(minutes=60) > now:
            secs = (t - now).total_seconds()
            if 0 < secs < 5.5 * 3600:
                print(f'Waiting until {hm} ET', flush=True)
                time.sleep(secs)
            return


def main(dry):
    wk, roster = load()
    stuck = []
    for b in [p for p in roster if p['slot'] not in (BENCH, IR) and p['dead'] and not p['locked']]:
        moves = fix(b, roster)
        if not moves:
            stuck.append(b['name'])
            print(f"STUCK: {b['name']} ({SLOT[b['slot']]}) is out or on bye and no healthy bench player can take the slot")
            continue
        print(('WOULD MOVE: ' if dry else 'MOVED: ') +
              '; '.join(f"{p['name']} {SLOT[a]}->{SLOT[z]}" for p, a, z in moves))
        if not dry:
            write(wk, moves)
        for p, _, z in moves:
            p['slot'] = z
    if not dry:
        wk, roster = load()
    starters = sorted((p for p in roster if p['slot'] not in (BENCH, IR)), key=lambda p: p['slot'])
    print(f'Week {wk} starters: ' + ', '.join(f"{SLOT[p['slot']]} {p['name']} {p['proj']:.1f}" for p in starters))
    if not stuck:
        print('OK: no OUT or bye starters left that can still be moved')
    return 3 if stuck else 0


if __name__ == '__main__':
    if '--wait' in sys.argv:
        wait()
    sys.exit(main('--dry-run' in sys.argv))
