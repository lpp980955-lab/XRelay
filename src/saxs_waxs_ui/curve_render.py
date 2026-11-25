"""Qt-free rendering/export from a GUI-thread snapshot of selected curves."""
import csv,hashlib,json,io
from pathlib import Path
import numpy as np
from .rendering import publication_render_context


def snapshot_plot(panel):
    from .curve_style import curve_appearance,curve_label,legend_visible
    transformed,labels=panel._transformed_curves()
    if not transformed or labels is None:raise ValueError('当前没有可导出的预览曲线')
    return dict(curves=transformed,labels=labels,style=dict(panel._curve_style),
        appearance=[curve_appearance(panel,i) for i in range(len(transformed))],
        legend_labels=[curve_label(panel,i) for i in range(len(transformed))],
        legend_visible=legend_visible(panel),legend_anchor=getattr(panel,'_legend_manual_anchor',None),
        bounds=[list(values) for values in panel.plot.viewRange()],
        frame=panel.four_frame_check.isChecked(),grid=panel.grid_check.isChecked(),
        focus=panel._focused_curve_index,rc_style=panel._publication_matplotlib_style(),
        axis_metadata=[panel._export_axis_metadata(curve,labels) for _,curve,_,_ in transformed])


def render_plot(snapshot,width_mm=180.,height_mm=130.,dpi=600):
    from matplotlib.figure import Figure
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.ticker import MultipleLocator,AutoMinorLocator,NullLocator
    from .curve_style import MPL_LINES
    s=snapshot['style']
    with publication_render_context(snapshot['rc_style']):
        figure=Figure(figsize=(width_mm/25.4,height_mm/25.4),dpi=dpi,facecolor='white',constrained_layout=True)
        FigureCanvasAgg(figure);axis=figure.add_subplot(111)
        for index,((_name,_curve,x,y),appearance) in enumerate(zip(snapshot['curves'],snapshot['appearance'])):
            focused=snapshot['focus'] is None or snapshot['focus']==index
            axis.plot(x,y,color=appearance['color'],linewidth=appearance['width']*(1.5 if snapshot['focus'] is not None and focused else 1.),
                linestyle=MPL_LINES[appearance['style']],alpha=1. if focused else .18,
                label=snapshot['legend_labels'][index],solid_capstyle='round',solid_joinstyle='round')
        axis.set_xlabel(s['x_title'] or snapshot['labels'][0],labelpad=7)
        axis.set_ylabel(s['y_title'] or snapshot['labels'][1],labelpad=8)
        if s['title']:axis.set_title(s['title'],fontsize=s['label_size'],fontweight='bold' if s['label_bold'] else 'normal')
        axis.set_xlim(*snapshot['bounds'][0]);axis.set_ylim(*snapshot['bounds'][1])
        sides={side:s['ticks_'+side] for side in ('bottom','left','top','right')}
        axis.tick_params(which='major',direction='in' if s['ticks_in'] else 'out',**sides,
            length=s['major_length'],width=s['tick_width'],pad=5)
        if s['x_step']>0:axis.xaxis.set_major_locator(MultipleLocator(s['x_step']))
        if s['y_step']>0:axis.yaxis.set_major_locator(MultipleLocator(s['y_step']))
        for target in (axis.xaxis,axis.yaxis):
            target.set_minor_locator(AutoMinorLocator(int(s['minor_count'])+1) if s['minor_ticks'] else NullLocator())
        axis.tick_params(which='minor',direction='in' if s['ticks_in'] else 'out',**sides,
            length=s['minor_length'],width=s['tick_width'])
        for side,spine in axis.spines.items():
            spine.set_linewidth(s['axis_width']);spine.set_color(s['axis_color'])
            spine.set_visible(side in ('left','bottom') or snapshot['frame'] or sides[side])
        if snapshot['grid']:axis.grid(True,which='major',color='#B8B8B8',linewidth=.55,alpha=.25)
        else:axis.grid(False)
        if snapshot['legend_visible']:
            kwargs=dict(loc=s['legend_position'] if s['legend_position']!='outside' else 'upper center',
                frameon=s['legend_frame'],ncol=int(s['legend_columns']),handlelength=2.4,handletextpad=.7,
                labelspacing=.45,borderaxespad=.8,prop=dict(family=[s['font'],'Microsoft YaHei','DejaVu Sans'],size=s['legend_size'],weight='bold' if s['legend_bold'] else 'normal'))
            if s['legend_position']=='outside':kwargs['bbox_to_anchor']=(.5,-.18)
            elif snapshot.get('legend_anchor'):
                kwargs.update(loc='upper left',bbox_to_anchor=snapshot['legend_anchor'],borderaxespad=0.)
            axis.legend(**kwargs)
        return figure,axis,dpi


def preview_png(snapshot,width_mm,height_mm):
    dpi=max(1,round(2800/(max(width_mm,height_mm)/25.4)))
    figure,_axis,_dpi=render_plot(snapshot,width_mm,height_mm,dpi)
    try:
        stream=io.BytesIO()
        with publication_render_context(snapshot['rc_style']):figure.savefig(stream,format='png',dpi=dpi)
        return stream.getvalue()
    finally:figure.clear()


def save_curve_export(snapshot,target,*,content='data',width_mm=180.,height_mm=130.,dpi=600,document=None):
    """Use existing workbook structure and displayed CSV XY-pair semantics."""
    target=Path(target);suffix=target.suffix.lower();target.parent.mkdir(parents=True,exist_ok=True)
    if content=='images':
        figure,_axis,_dpi=render_plot(snapshot,width_mm,height_mm,dpi)
        try:
            options=dict(dpi=dpi)
            if suffix in ('.tif','.tiff'):options['pil_kwargs']={'compression':'tiff_lzw'}
            with publication_render_context(snapshot['rc_style']):figure.savefig(target,**options)
        finally:figure.clear()
    elif suffix=='.xlsx':
        from saxs_waxs_core.workbook import write_workbook
        groups=[];records=[];contexts=[]
        for (name,curve,x,y),metadata in zip(snapshot['curves'],snapshot['axis_metadata']):
            groups.append((name,curve.x,curve.intensity,'原始横坐标','原始强度',curve.x_unit,curve.intensity_unit))
            if not (np.array_equal(curve.x,x,equal_nan=True) and np.array_equal(curve.intensity,y,equal_nan=True)):
                xname,xunit,yname,yunit=metadata
                groups.append((name+' · 显示坐标',x,y,xname,yname,xunit,yunit))
            records.append({'样品':name,'积分类型':curve.kind,'点数':len(curve.x),'横坐标单位':curve.x_unit,'强度单位':curve.intensity_unit})
            contexts.append((name,dict(curve.audit)))
        write_workbook(target,groups,records,contexts)
    elif suffix in ('.csv','.tsv'):
        with target.open('w',encoding='utf-8-sig',newline='') as stream:
            writer=csv.writer(stream,delimiter=',' if suffix=='.csv' else '\t')
            names=[];units=[]
            for (name,_curve,_x,_y),(xn,xu,yn,yu) in zip(snapshot['curves'],snapshot['axis_metadata']):
                names.extend((f'{name} | {xn} (X)',f'{name} | {yn} (Y)'));units.extend((xu,yu))
            writer.writerow(names);writer.writerow(units)
            for i in range(max(len(x) for _,_,x,_ in snapshot['curves'])):
                row=[]
                for _,_,x,y in snapshot['curves']:
                    for values in (x,y):row.append(float(values[i]) if i<len(values) and np.isfinite(values[i]) else None)
                writer.writerow(row)
    else:raise ValueError('不支持的曲线导出格式')
    if document is not None:
        with target.open('rb') as stream:digest=hashlib.file_digest(stream,'sha256').hexdigest()
        document=dict(document,output_file=target.name,output_sha256=digest,
            plot_appearance=snapshot['style'],legend_labels=snapshot['legend_labels'],legend_anchor=snapshot.get('legend_anchor'))
        if content=='images':document['page']=dict(width_mm=width_mm,height_mm=height_mm,dpi=dpi)
        if content=='images' or suffix!='.xlsx':
            sidecar=target.with_name(f'{target.stem}_{suffix[1:]}_parameters.json')
            sidecar.write_text(json.dumps(document,ensure_ascii=False,indent=2),encoding='utf-8')
    return target
