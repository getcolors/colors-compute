import {test,expect} from 'bun:test';
import {backend_plan} from '../src/rendering.ts';
const opts={'oci-ocpus':1,'provider-backend':'oci','provider-compute':'oci','oci-bucket':'demo-states','oci-region':'eu-frankfurt-1','oci-namespace':'namespace1','oci-compartment-id':'ocid1.compartment.example',profile:'demo','oci-bucket-mode':'managed','compute-prevent-destroy':false};
test('OCI compatibility backend isolates credentials and has a native identity',()=>{
 const plan=backend_plan(opts,'demo/compute/shared.tfstate');expect(plan.config.terraform.backend.s3.endpoints.s3).toBe('https://namespace1.compat.objectstorage.eu-frankfurt-1.oraclecloud.com');expect(plan.credential_bindings).toEqual({COLORS_PAR_OCI_ACCESS_KEY_ID:'access_key',COLORS_PAR_OCI_SECRET_ACCESS_KEY:'secret_key'});
});
