/** Snapshot workflow data while retaining opaque SDK callbacks. */
export function copy<T>(value:T, seen=new Map<object,unknown>()):T {
  if(value===null||typeof value!=='object')return value;
  if(seen.has(value))return seen.get(value) as T;
  if(Array.isArray(value)){
    const result:unknown[]=[];seen.set(value,result);
    for(const item of value)result.push(copy(item,seen));
    return result as T;
  }
  const proto=Object.getPrototypeOf(value);
  if(proto!==Object.prototype&&proto!==null)return value;
  const result:Record<string,unknown>={};seen.set(value,result);
  for(const [key,item] of Object.entries(value))Object.defineProperty(result,key,{value:copy(item,seen),enumerable:true,writable:true,configurable:true});
  return result as T;
}
