(ns io.github.getcolors.compute-controller
  (:require [cheshire.core :as json] [clojure.java.io :as io] [clojure.string :as str]))
(defn- fail [message] (throw (ex-info message {})))
(defn controller-artifact [opts shared]
  (let [descriptors (json/parse-string (slurp (io/resource "colors_compute/controllers.json")) true)
        provider (:provider-compute opts) d (when (string? provider) (get descriptors (keyword provider)))]
    (when-not d (fail "unsupported compute Kubernetes controller capability"))
    (when (and (contains? opts (keyword (:enabled_option d))) (not (true? (get opts (keyword (:enabled_option d)))))) (fail "Kubernetes controller must be enabled"))
    (let [version (:value (some #(when (some? (get opts (keyword %))) {:value (get opts (keyword %))}) (:version_options d)))
          params (:params shared) network (get params (keyword (:network_output d)))
          cluster-name (or (get opts (keyword (str provider "-name"))) (:profile opts))]
      (when-not (and (string? version) (re-matches #"v[0-9]+\.[0-9]+\.[0-9]+" version)) (fail "invalid Kubernetes controller version"))
      (when-not (and (map? params) (= provider (:provider params)) (string? network) (re-matches #"[A-Za-z0-9][A-Za-z0-9_-]{0,127}" network)) (fail "invalid Kubernetes controller shared network"))
      (when-not (and (string? cluster-name) (re-matches #"[A-Za-z0-9][A-Za-z0-9_-]{0,62}" cluster-name)) (fail "invalid Kubernetes controller cluster name"))
      {:filename "colors-compute-controller.yml" :content (-> (:content d) (str/replace "@VERSION@" version) (str/replace "@NETWORK_ID@" network))
       :credentials (:credentials d) :namespace (:namespace d) :rollout_resource (:rollout_resource d) :cluster_name cluster-name})))
