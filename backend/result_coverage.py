"""Separate saved calculation, requested display and variable coverage."""
def attach(result, request):
    if result.get('modelFamily')!='grid-continuous-v1':return result
    rows=result.get('daily') or []
    def coverage(key):
        valid=[row['date'][:10] for row in rows if row.get(key) is not None]
        return {'start':valid[0] if valid else None,'end':valid[-1] if valid else None,'validDays':len(valid)}
    result['timeZone']='Asia/Shanghai'
    result['calculationCoverage']={'start':result['start'],'end':result['end'],'hours':result['hours']}
    result['displayCoverage']={'start':request.get('evaluationStart') or result['start'],
                               'endExclusive':request.get('evaluationEnd') or result['end']}
    result['variableCoverage']={key:coverage(key) for key in ('total','duja','lakeLevel','observedLevel','observedFlow')}
    result['timeSemantics']={'dailyFlux':'24 hourly end-interval values integrated over the local business day',
                             'dailyFlow':'mean of the 24 complete hourly end-interval values',
                             'dailyLakeLevel':'mean of 24 complete hourly states; noon-initialized day is incomplete',
                             'dailyEndState':'state at next local midnight'}
    return result
