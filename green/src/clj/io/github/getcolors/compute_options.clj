(ns io.github.getcolors.compute-options
 (:require [cheshire.core :as json] [clojure.java.io :as io]))
(defn- fail [message] (throw (ex-info message {})))
(defn apply-options
 ([provider stage request documents] (apply-options provider stage request documents {}))
 ([provider stage request documents opts]
 (let [descriptor (get (json/parse-string (slurp (io/resource "colors_compute/compute-options.json")) true) (keyword provider))
       option (some-> (:backups_option descriptor) keyword)
       request (if (and (not (contains? request :backups)) option (contains? opts option)) (assoc request :backups {:enabled (get opts option)}) request)
       selected (select-keys request [:backups :ipv6])]
  (if (empty? selected) documents
   (let [descriptor (get (json/parse-string (slurp (io/resource "colors_compute/compute-options.json")) true) (keyword provider)) backup (:backups selected)]
    (when-not descriptor (fail "unsupported compute options capability"))
    (when (and (contains? selected :ipv6) (not (:ipv6_field descriptor))) (fail "unsupported compute options capability"))
    (when (and (contains? selected :ipv6) (not (boolean? (:ipv6 selected)))) (fail "invalid compute IPv6 policy"))
    (when (contains? selected :backups)
     (when-not (and (map? backup) (boolean? (:enabled backup)) (= (set (keys backup)) (if (and (:enabled backup) (:schedule_field descriptor)) #{:enabled :schedule} #{:enabled}))) (fail "invalid compute backup policy"))
     (when (and (:enabled backup) (:schedule_field descriptor))
      (let [schedule (:schedule backup) hour (:hour schedule)]
       (when-not (and (map? schedule) (= #{:type :hour} (set (keys schedule))) (= "daily" (:type schedule)) (number? hour) (<= 0 hour 23) (== hour (long hour))) (fail "invalid compute backup schedule")))))
    (if (not= stage "node") documents
     (let [paths (keep (fn [[filename doc]]
             (let [resource-key (if (contains? doc :resource) :resource "resource")
                   resources (get doc resource-key)
                   type-key (if (contains? resources (keyword (:resource_type descriptor))) (keyword (:resource_type descriptor)) (:resource_type descriptor))
                   instances (get resources type-key)
                   name-key (if (contains? instances (keyword (:resource_name descriptor))) (keyword (:resource_name descriptor)) (:resource_name descriptor))
                   path [filename resource-key type-key name-key]]
              (when (get-in documents path) path))) documents)]
      (when-not (= 1 (count paths)) (fail "compute options resource unavailable"))
      (update-in documents (first paths) (fn [resource]
       (cond-> resource
        (contains? selected :ipv6) (assoc (keyword (:ipv6_field descriptor)) (:ipv6 selected))
        (contains? selected :backups) (assoc (keyword (:backups_field descriptor)) (get (:backups_values descriptor) (keyword (str (:enabled backup)))))
        (and (:enabled backup) (:schedule_field descriptor)) (assoc (keyword (:schedule_field descriptor)) [{:type "daily" :hour (long (get-in backup [:schedule :hour]))}])))))))))
)
)
