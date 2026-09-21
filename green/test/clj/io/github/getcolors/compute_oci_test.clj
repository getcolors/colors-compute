(ns io.github.getcolors.compute-oci-test
 (:require [clojure.test :refer :all] [cheshire.core :as json]
           [io.github.getcolors.compute :as compute] [io.github.getcolors.compute-oci :as oci]))
(def opts {:oci-ocpus 1 :provider-backend "oci" :provider-compute "oci" :oci-bucket "demo-states" :oci-region "eu-frankfurt-1" :oci-namespace "namespace1" :oci-compartment-id "ocid1.compartment.example" :profile "demo" :oci-bucket-mode "managed" :compute-prevent-destroy false})
(deftest domain-placement
 (let [configured (assoc opts :oci-availability-domain "legacy" :oci-availability-domains ["ad1" "ad2" "ad3"])]
  (is (= "ad3" (:oci-availability-domain (oci/place-node configured "node" "broker-2"))))
  (is (= "ad1" (:oci-availability-domain (oci/place-node configured "node" "3"))))
  (is (= configured (oci/place-node configured "shared" "shared")))
  (doseq [domains [[] ["ad1" "ad1"] ["${injected}"] "ad1"]]
   (is (thrown-with-msg? Exception #"invalid OCI availability domains" (oci/place-node (assoc configured :oci-availability-domains domains) "node" "0"))))))
