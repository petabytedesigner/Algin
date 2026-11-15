"""Local pure order guard; broker metadata checks are additional."""
import math

class OrderValidation:
    def validate(self,instrument,direction,units,sl_price,tp_price,price,min_distance):
        errors=[]
        side={'LONG':1,'BUY':1,'SHORT':-1,'SELL':-1}.get(str(direction).upper())
        if side is None: errors.append('Unknown direction')
        for name,value in [('units',units),('sl',sl_price),('tp',tp_price),('price',price),('distance',min_distance)]:
            try:
                if not math.isfinite(float(value)): errors.append(name+' is not finite')
            except (TypeError,ValueError): errors.append(name+' is invalid')
        if errors:return {'is_valid':False,'errors':errors}
        if not instrument or not isinstance(instrument,str):errors.append('Invalid instrument')
        if int(float(units))!=float(units) or float(units)==0 or (side and side*float(units)<=0):errors.append('Units/direction mismatch')
        if min(float(sl_price),float(tp_price),float(price))<=0 or float(min_distance)<0:errors.append('Price or distance invalid')
        if side==1 and not (sl_price<price<tp_price):errors.append('Invalid LONG stop/target')
        if side==-1 and not (tp_price<price<sl_price):errors.append('Invalid SHORT stop/target')
        if min(abs(price-sl_price),abs(price-tp_price))<min_distance:errors.append('Stop/target distance too small')
        return {'is_valid':not bool(errors),'errors':errors}
