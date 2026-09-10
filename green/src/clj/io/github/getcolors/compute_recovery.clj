(ns io.github.getcolors.compute-recovery
  "Operator-invoked recovery of a failed initial shared AWS create."
  (:require [cheshire.core :as json] [clojure.string :as str]
            [io.github.getcolors.compute-coordinator :as coordinator]
            [io.github.getcolors.compute-execution :as execution]
            [io.github.getcolors.compute-runtime :as runtime])
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
