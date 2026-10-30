#!/usr/bin/env python3
"""I.R.I.D.E. — OPERA ODIM decoder, cell detection and experimental tracking."""
from __future__ import annotations
import json, re, sys, traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote
import xml.etree.ElementTree as ET
import h5py, numpy as np, requests
from pyproj import CRS, Transformer
from scipy import ndimage

ROOT=Path(__file__).resolve().parents[1]
DATA=ROOT/"data"
HIST=DATA/"iride_history.json"
S3="https://s3.waw3-1.cloudferro.com"
BUCKET="openradar-24h"
PREFIX="OPERA/COMP"
TIMEOUT=35
MAX_BYTES=250_000_000
THRESHOLD=float(__import__("os").getenv("MIN_DBZ","35"))
MIN_PIXELS=int(__import__("os").getenv("MIN_CELL_PIXELS","8"))
MAX_CELLS=100
MATRIX_SIDE=36
UA="IRIDE-experimental-nowcasting/1.0"

def now():
    return datetime.now(timezone.utc)

def stamp(d):
    return d.astimezone(timezone.utc).isoformat().replace("+00:00","Z")

def attr(g,k,default=None):
    if g is None or k not in g.attrs:
        return default
    v=g.attrs[k]
    if isinstance(v,bytes):
        return v.decode("utf-8","replace")
    if isinstance(v,np.ndarray) and v.shape==():
        v=v.item()
    return v.decode("utf-8","replace") if isinstance(v,bytes) else v

def number(v,default=None):
    try:
        x=float(np.asarray(v).item())
        return x if np.isfinite(x) else default
    except Exception:
        return default

def save(path,obj):
    path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_suffix(path.suffix+".tmp")
    tmp.write_text(
        json.dumps(obj,ensure_ascii=False,separators=(",",":"))+"\n",
        encoding="utf-8"
    )
    tmp.replace(path)

def list_day(day):
    prefix=f"{day:%Y/%m/%d}/{PREFIX}/"
    r=requests.get(
        f"{S3}/{BUCKET}/",
        params={"list-type":"2","prefix":prefix},
        timeout=TIMEOUT,
        headers={"User-Agent":UA}
    )
    r.raise_for_status()
    root=ET.fromstring(r.content)
    ns={"s":"http://s3.amazonaws.com/doc/2006-03-01/"}
    out=[]
    for x in root.findall("s:Contents",ns):
        key=x.findtext("s:Key",default="",namespaces=ns)
        size=int(x.findtext("s:Size",default="0",namespaces=ns))
        if key.endswith("@0@DBZH.h5") and 0<size<=MAX_BYTES:
            out.append((key,size))
    return out

def latest():
    t=now()
    items=[]
    for n in range(3):
        d=(t-timedelta(days=n)).date()
        try:
            items+=list_day(datetime(d.year,d.month,d.day,tzinfo=timezone.utc))
        except Exception as e:
            print("Listing",d,e,file=sys.stderr)
    if not items:
        raise RuntimeError("Nessun OPERA DBZH trovato negli ultimi tre giorni UTC.")
    def keytime(x):
        m=re.search(r"OPERA@(\d{8}T\d{4})@0@DBZH\.h5$",x[0])
        return m.group(1) if m else x[0]
    return max(items,key=keytime)

def fetch(key,size):
    p=DATA/"raw/opera_latest.h5"
    p.parent.mkdir(parents=True,exist_ok=True)
    url=f"{S3}/{BUCKET}/"+quote(key,safe="/@")
    total=0
    with requests.get(
        url,stream=True,timeout=TIMEOUT,headers={"User-Agent":UA}
    ) as r:
        r.raise_for_status()
        with p.open("wb") as f:
            for chunk in r.iter_content(1024*1024):
                if chunk:
                    total+=len(chunk)
                    if total>MAX_BYTES:
                        raise RuntimeError("Download oltre il limite di sicurezza.")
                    f.write(chunk)
    if size and total!=size:
        raise RuntimeError(f"Download incompleto: {total}/{size} byte.")
    return p,url

def decode(path):
    with h5py.File(path,"r") as h:
        if "dataset1/data1/data" not in h:
            raise RuntimeError("Dataset /dataset1/data1/data assente.")
        raw=h["dataset1/data1/data"][()]
        w=h.get("dataset1/data1/what")
        quantity=str(attr(w,"quantity",""))
        if quantity.upper()!="DBZH":
            raise RuntimeError(f"quantity non è DBZH: {quantity!r}")
        gain=number(attr(w,"gain"),1)
        offset=number(attr(w,"offset"),0)
        nd=number(attr(w,"nodata"))
        ud=number(attr(w,"undetect"))
        z=raw.astype(np.float32)*gain+offset
        bad=~np.isfinite(z)
        if nd is not None:
            bad|=raw==nd
        if ud is not None:
            bad|=raw==ud
        z[bad]=np.nan

        wh=h.get("where")
        if wh is None:
            raise RuntimeError("Gruppo /where assente.")
        proj=attr(wh,"projdef")
        xs=number(attr(wh,"xscale"))
        ys=number(attr(wh,"yscale"))
        nx=int(number(attr(wh,"xsize"),z.shape[1]))
        ny=int(number(attr(wh,"ysize"),z.shape[0]))
        if not proj or not xs or not ys or z.shape!=(ny,nx):
            raise RuntimeError("Metadati griglia incompleti o dimensioni incoerenti.")
        llon=number(attr(wh,"LL_lon"))
        llat=number(attr(wh,"LL_lat"))
        if llon is None or llat is None:
            raise RuntimeError("LL_lon/LL_lat assenti: georeferenziazione non sicura.")

        crs=CRS.from_user_input(str(proj))
        fwd=Transformer.from_crs(4326,crs,always_xy=True)
        inv=Transformer.from_crs(crs,4326,always_xy=True)
        ex,ey=fwd.transform(llon,llat)
        xo,yo=ex+xs/2,ey+ys/2

        errs=[]
        for lonkey,latkey,col,row in (
            ("LL_lon","LL_lat",0,ny-1),
            ("LR_lon","LR_lat",nx-1,ny-1),
            ("UL_lon","UL_lat",0,0),
            ("UR_lon","UR_lat",nx-1,0)
        ):
            lon=number(attr(wh,lonkey))
            lat=number(attr(wh,latkey))
            if lon is None or lat is None:
                continue
            px,py=fwd.transform(lon,lat)
            gx=xo+col*xs-xs/2
            gy=yo+(ny-1-row)*ys-ys/2
            errs.append(float(np.hypot(px-gx,py-gy)))

        if errs and max(errs)>max(abs(xs),abs(ys))*3:
            raise RuntimeError(
                "Controllo georeferenziazione fallito: angoli e passo griglia incoerenti."
            )

        pw=h.get("dataset1/what")
        ed,et=attr(pw,"enddate"),attr(pw,"endtime")
        product_time=(
            datetime.strptime(str(ed)+str(et),"%Y%m%d%H%M%S")
            .replace(tzinfo=timezone.utc)
            if ed and et else None
        )
        return z,{
            "projection":str(proj),
            "xsize":nx,
            "ysize":ny,
            "xscale_m":xs,
            "yscale_m":ys,
            "corner_check_max_error_m":round(max(errs),2) if errs else None,
            "license":str(attr(h.get("how"),"license",""))
        },inv,(xo,yo),product_time

def haversine(a,b):
    from math import radians,sin,cos,asin,sqrt
    p1,p2=radians(a["lat"]),radians(b["lat"])
    dp=radians(b["lat"]-a["lat"])
    dl=radians(b["lon"]-a["lon"])
    q=sin(dp/2)**2+cos(p1)*cos(p2)*sin(dl/2)**2
    return 6371*2*asin(min(1,sqrt(q)))

def history_read():
    try:
        return json.loads(HIST.read_text(encoding="utf-8"))
    except Exception:
        return {}

def matrix(patch,component):
    h,w=patch.shape
    sy=max(1,int(np.ceil(h/MATRIX_SIDE)))
    sx=max(1,int(np.ceil(w/MATRIX_SIDE)))
    out=[]
    for y in range(0,h,sy):
        row=[]
        for x in range(0,w,sx):
            z=patch[y:y+sy,x:x+sx]
            m=component[y:y+sy,x:x+sx]
            v=z[m & np.isfinite(z)]
            row.append(round(float(v.max()),1) if v.size else 0)
        out.append(row)
    return out,sx,sy

def detect(z,grid,inv,origin,timestamp,hist):
    labels,_=ndimage.label(
        np.isfinite(z)&(z>=THRESHOLD),
        structure=np.ones((3,3),dtype=np.uint8)
    )
    objects=ndimage.find_objects(labels)
    cells=[]
    xo,yo=origin

    for lid,sl in enumerate(objects,1):
        if sl is None:
            continue
        ys,xs=sl
        component=labels[sl]==lid
        area=int(component.sum())
        if area<MIN_PIXELS:
            continue

        patch=z[sl]
        yy,xx=np.where(component)
        vals=patch[yy,xx]
        weights=np.maximum(vals-THRESHOLD,.1)
        cx=float(xs.start+np.average(xx,weights=weights))
        cy=float(ys.start+np.average(yy,weights=weights))
        gx=xo+cx*grid["xscale_m"]
        gy=yo+(grid["ysize"]-1-cy)*grid["yscale_m"]
        lon,lat=inv.transform(gx,gy)

        mat,sx,sy=matrix(patch,component)
        edge=component & ~ndimage.binary_erosion(
            component,structure=np.ones((3,3)),border_value=0
        )
        by,bx=np.where(edge)
        if len(bx)>80:
            take=np.linspace(0,len(bx)-1,80).astype(int)
            bx,by=bx[take],by[take]

        perimeter=[]
        for py,px in zip(by,bx):
            x=xo+(xs.start+int(px))*grid["xscale_m"]
            y=yo+(grid["ysize"]-1-(ys.start+int(py)))*grid["yscale_m"]
            lo,la=inv.transform(x,y)
            perimeter.append([round(float(la),4),round(float(lo),4)])

        cells.append({
            "id":f"OPERA-{lid:05d}",
            "lat":round(float(lat),4),
            "lon":round(float(lon),4),
            "max_dbz":round(float(vals.max()),1),
            "mean_dbz":round(float(vals.mean()),1),
            "threshold_dbz":THRESHOLD,
            "area_pixels":area,
            "approx_area_km2":round(
                area*abs(grid["xscale_m"]*grid["yscale_m"])/1e6,1
            ),
            "bbox_pixels":[xs.start,ys.start,xs.stop,ys.stop],
            "perimeter":perimeter,
            "grid_matrix":mat,
            "matrix_sampling":{
                "x":sx,"y":sy,
                "original_rows":patch.shape[0],
                "original_cols":patch.shape[1]
            },
            "classification":"unclassified_radar_echo",
            "classification_note":
                "Il composito DBZH 2D non consente di diagnosticare da solo le classi convettive.",
            "severity":{"vil":None,"echo_top":None,"rain_mmh":None}
        })

    cells.sort(
        key=lambda c:(c["max_dbz"],c["area_pixels"]),
        reverse=True
    )
    cells=cells[:MAX_CELLS]
    used=set()

    for c in cells:
        best=None
        distance=80
        for tid,tr in hist.items():
            obs=tr.get("observations",[])
            if not obs or tid in used:
                continue
            try:
                last=obs[-1]
                age=(
                    timestamp-datetime.fromisoformat(
                        last["timestamp"].replace("Z","+00:00")
                    )
                ).total_seconds()/60
            except Exception:
                continue
            if 0<age<=90:
                d=haversine(last,c)
                if d<distance:
                    best,distance=tid,d

        tid=best or c["id"]
        used.add(tid)
        tr=hist.setdefault(tid,{"observations":[]})
        obs=tr.setdefault("observations",[])
        ts=stamp(timestamp)
        if not obs or obs[-1]["timestamp"]!=ts:
            obs.append({
                "timestamp":ts,
                "lat":c["lat"],
                "lon":c["lon"],
                "max_dbz":c["max_dbz"]
            })
            del obs[:-24]

        track={
            "track_id":tid,
            "observations":obs[-24:],
            "forecast_status":"insufficient_observations"
        }

        if len(obs)>=2:
            a,b=obs[-2],obs[-1]
            ta=datetime.fromisoformat(a["timestamp"].replace("Z","+00:00"))
            tb=datetime.fromisoformat(b["timestamp"].replace("Z","+00:00"))
            mins=(tb-ta).total_seconds()/60
            if mins>0:
                def pred(m):
                    f=m/mins
                    return [
                        round(b["lat"]+(b["lat"]-a["lat"])*f,5),
                        round(b["lon"]+(b["lon"]-a["lon"])*f,5)
                    ]
                track.update({
                    "forecast_status":"linear_extrapolation_from_two_observations",
                    "real_vector":[[a["lat"],a["lon"]],[b["lat"],b["lon"]]],
                    "forecast_30m":[[b["lat"],b["lon"]],pred(30)],
                    "forecast_60m":[[b["lat"],b["lon"]],pred(60)],
                    "observed_interval_minutes":round(mins,1),
                    "speed_kmh":round(haversine(a,b)/(mins/60),1)
                })

        c["track"]=track
        c["track_id"]=tid
        c["vectors"]={
            "real":track.get("real_vector"),
            "predictive_30m":track.get("forecast_30m"),
            "predictive_60m":track.get("forecast_60m"),
            "forecast_status":track["forecast_status"]
        }

    return cells,hist

def main():
    started=now()
    source=None
    try:
        key,size=latest()
        source,url=fetch(key,size)
        z,grid,inv,origin,pt=decode(source)

        if pt is None:
            m=re.search(r"OPERA@(\d{8}T\d{4})@0@DBZH\.h5$",key)
            if not m:
                raise RuntimeError("Timestamp del prodotto assente.")
            pt=datetime.strptime(
                m.group(1),"%Y%m%dT%H%M"
            ).replace(tzinfo=timezone.utc)

        hist=history_read()
        cells,hist=detect(z,grid,inv,origin,pt,hist)
        if not cells:
            raise RuntimeError(
                f"Nessuna cella ≥{THRESHOLD} dBZ con almeno {MIN_PIXELS} pixel; "
                "output precedente conservato."
            )

        payload={
            "schema_version":3,
            "product":"I.R.I.D.E. — Integrated Radar Imaging & Detection Engine",
            "status":"ok",
            "timestamp":stamp(pt),
            "generated_at":stamp(now()),
            "source":{
                "provider":"EUMETNET OPERA via public CloudFerro object storage",
                "url":url,
                "object_key":key,
                "product":"OPERA CIRRUS maximum reflectivity composite DBZH",
                "license":grid["license"],
                "attribution":
                    "Radar: EUMETNET OPERA; archivio: CloudFerro; elaborazione: I.R.I.D.E."
            },
            "grid":grid,
            "processing":{
                "threshold_dbz":THRESHOLD,
                "minimum_cell_pixels":MIN_PIXELS,
                "cell_count":len(cells),
                "method":"8-connected threshold segmentation",
                "forecast_method":
                    "linear extrapolation from two distinct observed centroids",
                "limits":[
                    "OPERA MAX DBZH è un composito 2D, non un volume radar 3D.",
                    "VIL, echo-top e rain rate non sono ricavati da questo prodotto.",
                    "Le classi MCS/MCC/MCV/squall line/V-shape/comma head/echo hook/supercella non vengono assegnate.",
                    "I vettori +30/+60 minuti sono estrapolazioni sperimentali, non previsioni ufficiali."
                ]
            },
            "cell_count":len(cells),
            "cells":cells
        }

        save(DATA/"iride.json",payload)
        save(ROOT/"docs/data/iride.json",payload)
        save(HIST,hist)
        save(DATA/"status.json",{
            "status":"ok",
            "timestamp":payload["timestamp"],
            "cell_count":len(cells),
            "source_object":key,
            "elapsed_seconds":round((now()-started).total_seconds(),1)
        })
        print(f"IRIDE OK: {len(cells)} regioni; {payload['timestamp']}")

    except Exception as e:
        save(DATA/"status.json",{
            "status":"error",
            "generated_at":stamp(now()),
            "message":str(e),
            "previous_data_preserved":True
        })
        traceback.print_exc()
        raise

    finally:
        if source:
            try:
                source.unlink(missing_ok=True)
            except Exception:
                pass

if __name__=="__main__":
    main()
