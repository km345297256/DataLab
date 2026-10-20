from __future__ import annotations
import base64,json,sys,math
from pathlib import Path
import numpy as np

def fail(msg): print(json.dumps({'success':False,'error':msg},ensure_ascii=False)); raise SystemExit(0)
def emit(data,out=None):
    payload={'success':True,**data}
    if out: Path(out).parent.mkdir(parents=True,exist_ok=True); Path(out).write_text(json.dumps(payload,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(payload,ensure_ascii=False,default=str))
def read_table(path):
    import pandas as pd
    p=Path(path)
    if not p.is_file(): fail('输入文件不存在')
    try: return pd.read_csv(p)
    except Exception:
        try: return pd.read_csv(p,sep='\s+')
        except Exception as e: fail(f'表格解析失败: {e}')
def fits_data(path):
    try:
        from astropy.io import fits
        hdul=fits.open(path,memmap=False); return hdul
    except Exception as e: fail(f'无法读取 FITS: {e}')
def image(path):
    hdul=fits_data(path)
    for hdu in hdul:
        if getattr(hdu,'data',None) is not None and np.asarray(hdu.data).ndim>=2:
            arr=np.asarray(hdu.data,dtype=float); arr=np.squeeze(arr)
            while arr.ndim>2: arr=arr[0]
            return hdul,hdu,arr
    fail('FITS 中没有二维图像数据')
def stats(values):
    v=np.asarray(values,dtype=float); finite=v[np.isfinite(v)]
    return {'count':int(v.size),'valid_count':int(finite.size),'missing_count':int(v.size-finite.size),**({'min':float(np.min(finite)),'max':float(np.max(finite)),'mean':float(np.mean(finite)),'std':float(np.std(finite)),'median':float(np.median(finite))} if finite.size else {})}
def main():
    if len(sys.argv)!=3: fail('参数错误')
    op=sys.argv[1]
    try: args=json.loads(base64.urlsafe_b64decode(sys.argv[2]+'='*(-len(sys.argv[2])%4)).decode())
    except Exception: fail('参数编码无效')
    out=args.get('output_path'); path=args.get('input_path')
    if op=='space_fits_inspect':
        hdul=fits_data(path); items=[]
        for i,h in enumerate(hdul): items.append({'index':i,'type':h.__class__.__name__,'shape':list(np.shape(h.data)) if getattr(h,'data',None) is not None else None,'dtype':str(getattr(h.data,'dtype',None)) if getattr(h,'data',None) is not None else None,'header_keys':len(h.header)})
        emit({'file':Path(path).name,'hdu_count':len(hdul),'primary_header':{k:str(hdul[0].header[k]) for k in list(hdul[0].header)[:80]},'hdus':items}); return
    if op=='space_fits_wcs':
        hdul=fits_data(path); from astropy.wcs import WCS
        w=WCS(hdul[0].header); emit({'file':Path(path).name,'celestial':bool(w.has_celestial),'naxis':w.naxis,'wcs':w.to_header(relax=True).__str__()},out); return
    if op in {'space_fits_preview','space_fits_crop','space_sky_coordinate_convert','space_source_detect','space_aperture_photometry'}:
        hdul,hdu,arr=image(path)
        if op=='space_fits_crop':
            x0,y0,w,h=map(int,(args['x0'],args['y0'],args['width'],args['height'])); cropped=arr[y0:y0+h,x0:x0+w]
            if cropped.size==0: fail('裁剪窗口为空')
            from astropy.io import fits; Path(out).parent.mkdir(parents=True,exist_ok=True); fits.PrimaryHDU(cropped,header=hdu.header).writeto(out,overwrite=True); emit({'output_path':Path(out).name,'shape':list(cropped.shape)}); return
        if op=='space_sky_coordinate_convert':
            from astropy.wcs import WCS; from astropy.coordinates import SkyCoord
            w=WCS(hdu.header); result={}
            if 'ra' in args and 'dec' in args: px=w.world_to_pixel(SkyCoord(args['ra'],args['dec'],unit='deg')); result={'x':float(px[0]),'y':float(px[1]),'ra':args['ra'],'dec':args['dec']}
            elif 'x' in args and 'y' in args: sky=w.pixel_to_world(args['x'],args['y']); result={'x':args['x'],'y':args['y'],'ra_deg':float(sky.ra.deg),'dec_deg':float(sky.dec.deg)}
            else: fail('需要 x/y 或 ra/dec')
            emit(result,out); return
        if op=='space_source_detect':
            from scipy.ndimage import label,center_of_mass
            finite=arr[np.isfinite(arr)]; threshold=float(np.nanmedian(arr))+float(args.get('threshold_sigma',5))*float(np.nanstd(arr)); labels,n=label(np.isfinite(arr)&(arr>threshold)); sources=[]
            for idx in range(1,n+1): mask=labels==idx; count=int(mask.sum());
            # bounded connected-component summary
            for idx in range(1,min(n,1000)+1):
                mask=labels==idx; yy,xx=np.where(mask); sources.append({'x':float(xx.mean()),'y':float(yy.mean()),'pixel_count':int(mask.sum()),'peak':float(np.nanmax(arr[mask]))})
            emit({'threshold':threshold,'source_count':len(sources),'sources':sources},out); return
        if op=='space_aperture_photometry':
            yy,xx=np.indices(arr.shape); r=np.hypot(xx-float(args['x']),yy-float(args['y'])); aperture=r<=float(args.get('radius',4)); ann=(r>=float(args.get('annulus_inner',6)))&(r<=float(args.get('annulus_outer',9))); bg=float(np.nanmedian(arr[ann])); flux=float(np.nansum(arr[aperture]-bg)); emit({'x':args['x'],'y':args['y'],'aperture_pixels':int(aperture.sum()),'background_median':bg,'net_flux':flux},out); return
        import matplotlib; matplotlib.use('Agg'); import matplotlib.pyplot as plt
        finite=arr[np.isfinite(arr)]; lo,hi=np.nanpercentile(finite,[1,99]); norm=np.clip((arr-lo)/(hi-lo+1e-12),0,1); stretch=args.get('stretch','linear'); norm=np.log1p(100*norm)/np.log1p(100) if stretch=='log' else np.sqrt(norm) if stretch=='sqrt' else norm
        Path(out).parent.mkdir(parents=True,exist_ok=True); plt.imsave(out,norm,cmap='gray'); emit({'output_path':Path(out).name,'shape':list(arr.shape),'stretch':stretch,'min':float(lo),'max':float(hi)}); return
    if op=='space_spectrum_extract':
        _,_,arr=image(path); axis=args.get('axis','row'); idx=int(args['index']); values=arr[idx,:] if axis=='row' else arr[:,idx]; emit({'axis':axis,'index':idx,'data':[None if not np.isfinite(v) else float(v) for v in values]},out); return
    if op=='space_spectrum_wavelength_calibrate':
        df=read_table(path); col=df.columns[-1]; px=np.arange(len(df)); wavelength=np.polynomial.polynomial.polyval(px,args['coefficients']); emit({'point_count':len(df),'wavelength_column':'wavelength','value_column':str(col),'data':[{'wavelength':float(x),'value':None if not np.isfinite(y) else float(y)} for x,y in zip(wavelength,df[col].to_numpy(float))]},out); return
    if op=='space_cdf_inspect':
        try:
            import cdflib; info=cdflib.cdf_info(path); emit({'file':Path(path).name,'variables':info.get('zVariables',[]),'attributes':info.get('Attributes',{}),'global_attributes':info.get('rVariables',[])})
        except Exception as e: fail(f'CDF 解析失败: {e}')
        return
    if op=='space_time_system_convert':
        from astropy.time import Time
        vals=[]
        for value in args['times']:
            t=Time(value,scale=args.get('from_scale','utc')); converted=getattr(t,args.get('to_scale','tai')); vals.append({'input':value,'output':converted.isot,'scale':args.get('to_scale','tai')})
        emit({'from_scale':args.get('from_scale','utc'),'to_scale':args.get('to_scale','tai'),'times':vals},out); return
    if op=='space_orbit_elements':
        r=np.array(args['position_km'],float); v=np.array(args['velocity_km_s'],float); mu=398600.4418; h=np.cross(r,v); evec=np.cross(v,h)/mu-r/np.linalg.norm(r); e=float(np.linalg.norm(evec)); energy=float(np.dot(v,v)/2-mu/np.linalg.norm(r)); a=-mu/(2*energy); i=math.degrees(math.acos(h[2]/np.linalg.norm(h))); emit({'semi_major_axis_km':a,'eccentricity':e,'inclination_deg':i,'angular_momentum_km2_s':float(np.linalg.norm(h))},out); return
    if op in {'space_tle_propagate','space_ground_track'}:
        try:
            from skyfield.api import EarthSatellite, load; lines=Path(path).read_text().splitlines(); lines=[x.strip() for x in lines if x.strip()]; l1,l2=(lines[-2],lines[-1]); sat=EarthSatellite(l1,l2,'PXP',load.timescale()); times=args.get('times') or [args.get('start_time')]; ts=load.timescale(); result=[]
            for value in times: t=ts.from_datetime(__import__('datetime').datetime.fromisoformat(value.replace('Z','+00:00'))); geoc=sat.at(t); sub=geoc.subpoint(); result.append({'time':value,'latitude_deg':float(sub.latitude.degrees),'longitude_deg':float(sub.longitude.degrees),'elevation_km':float(sub.elevation.km),'position_km':[float(x) for x in geoc.position.km]})
            emit({'satellite':sat.name,'points':result},out)
        except Exception as e: fail(f'TLE 解析失败: {e}')
        return
    if op in {'space_solar_wind_statistics','space_magnetic_disturbance','space_particle_spectrum','space_tec_analysis'}:
        df=read_table(path); numeric=df.select_dtypes(include='number'); payload={'file':Path(path).name,'columns':[str(c) for c in df.columns],'row_count':len(df),'statistics':{str(c):stats(df[c].to_numpy()) for c in numeric.columns}}
        if op=='space_magnetic_disturbance':
            window=max(3,int(args.get('window',31))); payload['disturbance']={str(c):[None if not np.isfinite(v) else float(v) for v in (df[c]-df[c].rolling(window,center=True,min_periods=1).median())] for c in numeric.columns}
        emit(payload,out); return
    if op=='space_quality_report':
        p=Path(path); payload={'file':p.name,'suffix':p.suffix.lower(),'size_bytes':p.stat().st_size}
        if p.suffix.lower() in {'.fits','.fit','.fts'}:
            hdul=fits_data(path); payload['hdu_count']=len(hdul); payload['image_shapes']=[list(np.shape(h.data)) for h in hdul if getattr(h,'data',None) is not None]
        else:
            df=read_table(path); payload.update({'row_count':len(df),'columns':list(map(str,df.columns)),'missing_by_column':{str(c):int(df[c].isna().sum()) for c in df.columns}})
        emit(payload,out); return
    fail('未知空间科学工具')
if __name__=='__main__': main()
