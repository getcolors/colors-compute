(ns io.github.getcolors.compute-recovery
  "Operator-invoked recovery of a failed initial shared AWS create."
  (:require [cheshire.core :as json] [clojure.string :as str]
            [io.github.getcolors.compute-coordinator :as coordinator]
            [io.github.getcolors.compute-execution :as execution]
            [io.github.getcolors.compute-runtime :as runtime]
            [io.github.getcolors.compute-oci :as oci])
  (:import [java.nio.file Files] [java.nio.file.attribute FileAttribute]))
(def scans [["describe-vpcs" "Vpcs" "tag:Name"] ["describe-subnets" "Subnets" "tag:Name"]
            ["describe-internet-gateways" "InternetGateways" "tag:Name"] ["describe-route-tables" "RouteTables" "tag:Name"]
            ["describe-security-groups" "SecurityGroups" "tag:Name"] ["describe-key-pairs" "KeyPairs" "key-name"]])
(defn- require-valid [ok message] (when-not ok (throw (ex-info message {}))))
(defn recover-absent-aws-shared!
  ([opts operation-id] (recover-absent-aws-shared! opts operation-id (into {} (System/getenv))))
  ([opts operation-id environment] (recover-absent-aws-shared! opts operation-id environment runtime/run-command
                                   (fn [opts env] (coordinator/coordinator opts env nil nil nil {:event-prefix "lifecycle/"}))))
  ([opts operation-id environment runner factory]
   (require-valid (= "aws" (:provider-compute opts)) "recovery requires AWS")
   (let [owner (factory opts environment)]
     (coordinator/acquire! owner)
     (try
       (let [doc (:document (coordinator/snapshot owner)) shared (:shared doc)]
         (require-valid (and (= "active" (:status doc)) (= "prepared" (get-in doc [:key :phase]))
                             (= "failed" (:phase shared)) (= "create" (:operation shared)) (= operation-id (:operation_id shared))
                             (every? #(= "declared" (:phase %)) (vals (:nodes doc)))) "recovery does not match failed initial create")
         (require-valid (= {:status "absent"} (execution/state-presence opts (str (:profile opts) "/compute/shared.tfstate") environment runner)) "recovery requires confirmed absent shared state")
         (let [directory (Files/createTempDirectory "colors-recovery-" (make-array FileAttribute 0))
               env (merge (into {} (remove (fn [[k _]] (re-find #"^(COLORS_PAR_|TF_|TOFU_)" k)) environment))
                          {"AWS_PAGER" "" "AWS_CLI_AUTO_PROMPT" "off" "AWS_MAX_ATTEMPTS" "1"})]
           (try
             (doseq [[operation collection filter-name] scans]
               (let [result (runner ["aws" "ec2" operation "--region" (:aws-region opts) "--filters" (str "Name=" filter-name ",Values=" (:profile opts) "*")
                                     "--query" (str "length(" collection ")") "--output" "json" "--no-cli-pager"] (str directory) env 120000)]
                 (require-valid (and (= 0 (:exit result)) (= 0 (json/parse-string (:out result)))) "recovery requires reviewed absent AWS resources")))
             (finally (Files/deleteIfExists directory))))
         (coordinator/transition! owner "shared-retry" {:evidence "verified-provider-absence"})
         {:status "recovered"})
       (finally (coordinator/release! owner))))))

(defn recover-absent-oci-nodes!
  "Retry explicitly selected failed OCI creates only after state and resource absence."
  ([opts operations] (recover-absent-oci-nodes! opts operations (into {} (System/getenv))))
  ([opts operations environment]
   (recover-absent-oci-nodes! opts operations environment runtime/run-command
                            (fn [opts env] (coordinator/coordinator opts env nil nil nil {:event-prefix "lifecycle/"}))))
  ([opts operations environment runner factory]
   (require-valid (and (= "oci" (:provider-compute opts)) (= "oci" (:provider-backend opts))
                       (map? operations) (seq operations)) "recovery requires OCI node operation IDs")
   (let [owner (factory opts environment)]
    (coordinator/acquire! owner)
    (try
     (let [doc (:document (coordinator/snapshot owner))]
      (require-valid (and (= "active" (:status doc)) (= "prepared" (get-in doc [:key :phase]))
                          (= "ready" (get-in doc [:shared :phase]))) "recovery requires owned OCI shared state")
      (doseq [[id operation-id] operations
              :let [record (get-in doc [:nodes (keyword (name id))])]]
       (require-valid (and (= "failed" (:phase record)) (= "create" (:operation record)) (= operation-id (:operation_id record))) "recovery does not match failed node create")
       (let [observed ((oci/client opts environment runner) "GET" (oci/object-path opts (:state_key record)) nil {} {})
             state (:data observed)]
        (require-valid (or (nil? observed) (and (runtime/valid-state? state) (= [] (:resources state)) (= {} (:outputs state)))) "recovery requires absent or empty node state")))
      (let [request (oci/client opts environment runner "iaas")
            domains (distinct (cons (:oci-availability-domain opts) (oci/availability-domains opts)))
            scopes (cons ["/20160918/instances" {}] (map (fn [domain] ["/20160918/bootVolumes" {:availabilityDomain domain}]) domains))]
       (doseq [[path filters] scopes]
        (loop [page nil]
         (let [response (request "GET" path nil (cond-> (assoc filters :compartmentId (:oci-compartment-id opts)) page (assoc :page page)) {})
               data (:data response)]
          (require-valid (and (vector? data)
                              (not-any? #(and (str/includes? (or (:displayName %) "") (:profile opts))
                                              (not= "TERMINATED" (:lifecycleState %))) data)) "recovery requires absent OCI instances and boot volumes")
          (when-let [next-page (get-in response [:headers :opc-next-page])] (recur next-page))))))
      (doseq [[id _] operations] (coordinator/transition! owner "retry" {:node_id (name id) :evidence "verified-provider-absence"}))
      {:status "recovered" :nodes (vec (sort (map name (keys operations))))})
     (finally (coordinator/release! owner))))))
