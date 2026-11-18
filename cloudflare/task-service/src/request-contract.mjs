const schema=(properties,required=Object.keys(properties))=>({type:'object',properties,required,additionalProperties:false});
const string={type:'string'};

/** Public POST /v1/tasks body. Model and mode may be filled by scenario routing. */
export const REQUEST={
  ...schema({
    mode:{enum:['agent','api','auto']},
    profile:{type:'string',minLength:1,maxLength:100,pattern:'^[^:]+$'},
    objective:{type:'string',minLength:1},
    model:string,
    materials:{type:'array'},
    output_schema:{type:'object'},
    timeout_seconds:{type:'integer',minimum:1},
    issue:{type:'object'},
    scenario:{type:'string',minLength:1,maxLength:64,pattern:'^[A-Za-z][A-Za-z0-9_-]{0,63}$'},
    complexity:{enum:['simple','standard','complex']},
    reasoning_effort:{enum:['low','medium','high']},
  },['objective','output_schema']),
  anyOf:[{required:['mode']},{required:['scenario']}],
};
