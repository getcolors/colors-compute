(ns io.github.getcolors.compute-endpoint
  (:require [cheshire.core :as json] [clojure.java.io :as io]))
(defn endpoint-agent [provider]
  (let [recipes (json/parse-string (slurp (io/resource "colors_compute/provider-recipes.json")) true)]
    (when-not (and (string? provider) (get-in recipes [(keyword provider) :application_reserved_ip]))
      (throw (ex-info "unsupported compute endpoint capability" {})))
    {:filename "colors-compute-endpoint" :content (slurp (io/resource "colors_compute/endpoint-agent.py")) :credentials (get-in recipes [(keyword provider) :endpoint_credentials])}))
