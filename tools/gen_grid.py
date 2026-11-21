"""Build map/solid_v2.i16: ground + building/stadium roof height (int16 cm) on the terrain grid.
Offline only (needs numpy): python3 tools/gen_grid.py arena map/solid_v2.i16
Bridges are skipped (they can be walked and shot under)."""
import json, numpy as np, sys
R=sys.argv[1]; OUT=sys.argv[2]
w=json.load(open(R+'/v2/world.json')); T=w['terrain']
nx,ny=T['nx'],T['ny']; x0,y0,st=T['minX'],T['minY'],T['step']
ter=np.fromfile(R+'/v2/terrain.i16','<i2').reshape(ny,nx).astype(np.float32)/100
top=ter.copy()
X=x0+np.arange(nx)*st; Y=y0+np.arange(ny)*st
def inpoly(ring, xs, ys):
    pts=np.array(ring).reshape(-1,2); res=np.zeros(xs.shape,bool)
    n=len(pts)
    for k in range(n):
        ax,ay=pts[k]; bx,by=pts[(k+1)%n]
        c=((ay>ys)!=(by>ys))
        with np.errstate(divide='ignore',invalid='ignore'):
            xi=ax+(ys-ay)*(bx-ax)/(by-ay)
        res ^= c & (xs<xi)
    return res
stats={}
nb=0
for p in w['prisms']:
    k=p['kind']; stats[k]=stats.get(k,0)+1
    pts=np.array(p['ring']).reshape(-1,2)
    i0=max(0,int((pts[:,0].min()-x0)/st)-1); i1=min(nx,int((pts[:,0].max()-x0)/st)+2)
    j0=max(0,int((pts[:,1].min()-y0)/st)-1); j1=min(ny,int((pts[:,1].max()-y0)/st)+2)
    xs,ys=np.meshgrid(X[i0:i1],Y[j0:j1])
    m=inpoly(p['ring'],xs,ys)
    for h in p.get('holes') or []:
        if len(h)>=6: m&=~inpoly(h,xs,ys)
    g=ter[j0:j1,i0:i1]
    if k=='bridge': continue
    if m.any() and p['base']>np.median(g[m])+3: nb+=1; continue  # overhang: skip
    sub=top[j0:j1,i0:i1]; sub[m]=np.maximum(sub[m],p['top'])
print(stats,'overhang skipped',nb)
diff=top-ter; print('cells covered',(diff>1.2).sum(),'max top',top.max())
np.round(top*100).astype('<i2').tofile(OUT)
