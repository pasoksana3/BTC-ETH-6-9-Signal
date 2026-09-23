import os, json, time, logging
from collections import OrderedDict
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import requests
from dotenv import load_dotenv
load_dotenv()
TOKEN=os.getenv('TELEGRAM_BOT_TOKEN','').strip(); CHAT_IDS=[x.strip() for x in os.getenv('TELEGRAM_CHAT_ID','').split(',') if x.strip()]
SYMBOLS=[x.strip().upper() for x in os.getenv('MEXC_SYMBOLS','ETH_USDT,BTC_USDT').split(',') if x.strip()]
POLL=int(os.getenv('POLL_SECONDS','15'))
logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(levelname)s | %(message)s'); log=logging.getLogger('bot')
def utc(ts): return datetime.fromtimestamp(ts,tz=timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
def kyiv(ts): return datetime.fromtimestamp(ts,tz=ZoneInfo('Europe/Kyiv')).strftime('%Y-%m-%d %H:%M')
def color(c): return 'GREEN' if c['close']>c['open'] else 'RED' if c['close']<c['open'] else 'DOJI'
def load_state(path):
    try:
        with open(path,encoding='utf8') as f:return json.load(f)
    except Exception:return {'last_processed_10m':None,'pending':[]}
def save_state(s,path):
    with open(path+'.tmp','w',encoding='utf8') as f:json.dump(s,f,indent=2)
    os.replace(path+'.tmp',path)
def tg(text,group_text=None):
    if not TOKEN or not CHAT_IDS:return False
    ok=0
    for cid in CHAT_IDS:
        try:
            r=requests.post(f'https://api.telegram.org/bot{TOKEN}/sendMessage',json={'chat_id':cid,'text':group_text if group_text is not None and cid.startswith('-') else text},timeout=15)
            if r.ok and r.json().get('ok'):ok+=1
        except Exception:log.exception('Telegram exception | %s',cid)
    return ok==len(CHAT_IDS)
def fetch(symbol):
    r=requests.get(f'https://api.mexc.com/api/v1/contract/kline/{symbol}',params={'interval':'Min1','limit':300,'_ts':int(time.time()*1000)},headers={'Cache-Control':'no-cache','Pragma':'no-cache','User-Agent':'ETH-BTC-10m-Signal-Bot/V5'},timeout=15); r.raise_for_status(); data=r.json().get('data')
    if not data:raise RuntimeError('MEXC empty response')
    out=[]
    if isinstance(data,dict) and isinstance(data.get('time'),list):
        for i,t in enumerate(data['time']):out.append({'ts':int(t),'open':float(data['open'][i]),'close':float(data['close'][i])})
    elif isinstance(data,list):
        for row in data:
            if isinstance(row,dict):out.append({'ts':int(row.get('time',row.get('t'))),'open':float(row.get('open',row.get('o'))),'close':float(row.get('close',row.get('c')))})
            else:out.append({'ts':int(row[0]),'open':float(row[1]),'close':float(row[2])})
    out.sort(key=lambda x:x['ts']); return out
def current_10m(mins):
    if not mins:return None
    b=(mins[-1]['ts']//600)*600; rows=[x for x in mins if (x['ts']//600)*600==b]
    if not rows:return None
    rows.sort(key=lambda x:x['ts']); return {'ts':b,'open':rows[0]['open'],'close':rows[-1]['close']}
def agg(mins):
    buckets=OrderedDict()
    for c in mins:buckets.setdefault((c['ts']//600)*600,[]).append(c)
    now=int(time.time()); out=[]
    for b,rows in buckets.items():
        if b+600<=now and len(rows)>=9:out.append({'ts':b,'open':rows[0]['open'],'close':rows[-1]['close']})
    return out
class Engine:
    def __init__(self,state,path,symbol):self.state=state;self.path=path;self.symbol=symbol;self.c=OrderedDict();self.pending=[];self.initialized=False;self.pre_alerted=set()
    def seed(self,closed):
        self.c=OrderedDict((x['ts'],x) for x in closed[-150:]);self.pending=[];self.state['pending']=[];self.state['last_processed_10m']=next(reversed(self.c)) if self.c else None;save_state(self.state,self.path);self.initialized=True
    def maybe_pre_alert(self,live):
        if not self.initialized or not live:return
        elapsed=time.time()-live['ts']
        if elapsed<540 or elapsed>=600:return
        keys=list(self.c); target=live['ts']; s_ts=target-8*600
        if s_ts not in self.c:return
        si=keys.index(s_ts); start=self.c[s_ts]; sc=color(start)
        if sc not in ('GREEN','RED'):return
        if si+1<len(keys) and color(self.c[keys[si+1]])==sc:return
        c6=target-3*600;c7=target-2*600;c8=target-600
        if c6 not in self.c or c7 not in self.c or c8 not in self.c:return
        trig=color(self.c[c6])
        if trig not in ('GREEN','RED') or trig==sc:return
        if color(self.c[c7])!=trig or color(self.c[c8])!=trig or color(live)!=trig:return
        if target in self.pre_alerted:return
        log.info('PRE-SIGNAL | %s | start=%s %s | #9=%s %s',self.symbol,utc(s_ts),sc,utc(target),trig)
        tg('',f'**Всі готові?**\n**Скоро дам СИГНАЛ!**\n\n{self.symbol.replace("_USDT","USDT")} Futures\nTimeframe: 10m\n\n⚠️ Сигнал буде тільки після закриття свічки.')
        self.pre_alerted.add(target)
    def ingest(self,closed):
        if not self.initialized:self.seed(closed);return 0
        known=set(self.c);new=[x for x in closed if x['ts'] not in known]
        for x in new:self.c[x['ts']]=x
        self.c=OrderedDict(sorted(self.c.items()))
        while len(self.c)>150:self.c.popitem(last=False)
        for x in new:self.evaluate(x['ts'])
        if new:self.state['pending']=self.pending;self.state['last_processed_10m']=new[-1]['ts'];save_state(self.state,self.path)
        return len(new)
    def evaluate(self,ts):
        keys=list(self.c);idx=keys.index(ts);cur=self.c[ts];keep=[]
        for p in self.pending:
            rel=idx-p['trigger_idx']; trig=p['trigger_color']
            if rel in (1,2,3):
                if color(cur)!=trig:log.info('CANCEL SETUP | %s | candle=%d changed color',self.symbol,rel+6)
                else:
                    if rel==3 and not p.get('signal_sent'):
                        d=p['direction']; sym=self.symbol.replace('_USDT','USDT'); txt=f'SIGNAL {d}\n\n{sym} Futures\nTimeframe: 10m\nStart: {kyiv(p["start_ts"])} Kyiv time\n\nSignal only - no automatic trading.\n\nТрейдер Василь Павлів\n@vasylpavliv\nhttps://t.me/vasylpavliv';tg(txt,txt);p['signal_sent']=True
                    keep.append(p)
                continue
            if rel<4:keep.append(p);continue
            want='GREEN' if p['direction']=='LONG' else 'RED'
            if rel<=10 and color(cur)==want:
                sym=self.symbol.replace('_USDT','USDT');tg(f'WIN\n{sym} Futures\nDirection: {p["direction"]}\nResult candle: {rel+6}\n\nТрейдер Василь Павлів\n@vasylpavliv\nhttps://t.me/vasylpavliv')
            elif rel>=10:
                sym=self.symbol.replace('_USDT','USDT');tg(f'LOSS\n{sym} Futures\nDirection: {p["direction"]}\nNo confirmation after signal\n\nТрейдер Василь Павлів\n@vasylpavliv\nhttps://t.me/vasylpavliv')
            else:keep.append(p)
        self.pending=keep
        if idx<6:return
        sidx=idx-6;start=self.c[keys[sidx]];sc=color(start)
        if sc not in ('GREEN','RED'):return
        if sidx+1<len(keys) and color(self.c[keys[sidx+1]])==sc:return
        trig=color(cur);d='LONG' if sc=='GREEN' and trig=='RED' else 'SHORT' if sc=='RED' and trig=='GREEN' else None
        if not d or any(p['start_ts']==start['ts'] for p in self.pending):return
        self.pending.append({'start_ts':start['ts'],'trigger_idx':idx,'trigger_color':trig,'direction':d,'signal_sent':False})
        log.info('TRIGGER CANDIDATE %s | %s | start=%s %s | trigger=%s %s | waiting for #7-#9',self.symbol,d,utc(start['ts']),sc,utc(ts),trig)
engines={}
for sym in SYMBOLS:
    path=os.getenv(f'STATE_FILE_{sym}',f'state_{sym}.json');engines[sym]=Engine(load_state(path),path,sym)
def main():
    log.info('Started BTCUSDT + ETHUSDT 10m signal bot V5 | SIGNAL after #9')
    while True:
        for sym,e in engines.items():
            try:
                mins=fetch(sym);live=current_10m(mins)
                if live:e.maybe_pre_alert(live)
                closed=agg(mins)
                if closed:e.ingest(closed)
            except Exception as ex:log.exception('LOOP ERROR %s: %s',sym,ex)
        time.sleep(POLL)
if __name__=='__main__':main()
