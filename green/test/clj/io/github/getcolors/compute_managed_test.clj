(ns io.github.getcolors.compute-managed-test
  (:require [clojure.test :refer [deftest is]] [cheshire.core :as json] [clojure.java.io :as io] [clojure.string :as str]
            [io.github.getcolors.compute-managed :as managed]
            [io.github.getcolors.compute-managed-access :as access]
            [io.github.getcolors.compute-managed-journal :as reducer]
            [io.github.getcolors.compute-runtime :as runtime]
            [io.github.getcolors.compute-execution :as execution]
            [io.github.getcolors.compute-coordinator :as coordinator]
            [io.github.getcolors.compute-coordinator-test :as ct])
  (:import [java.util Base64] [java.nio.file Files LinkOption]))
(def opts {:profile "demo" :provider-compute "vultr" :provider-backend "s3" :s3-bucket "states" :s3-region "eu-west-1"
           :vultr-region "ams" :vultr-vke-version "v1.35.1" :vultr-node-plan "vc2-2c-4gb" :vultr-node-count 2 :compute-prevent-destroy true})
(def params {:provider "vultr" :kind "managed-kubernetes" :name "demo" :cluster_id "cluster-123" :endpoint "https://192.0.2.1"})
(def config "apiVersion: v1\nkind: Config\nclusters: [{name: cluster, cluster: {server: https://192.0.2.1}}]\nusers: [{name: operator, user: {token: private-token}}]\ncontexts: [{name: active, context: {cluster: cluster, user: operator}}]\ncurrent-context: active\n")
(defn state-text ([] (state-text config)) ([config]
 (json/generate-string {:version 4 :serial 1 :lineage "fixture" :resources [{:type "vultr_kubernetes"}]
                       :outputs {:params {:value params :sensitive false} :kubeconfig_b64 {:value (.encodeToString (Base64/getEncoder) (.getBytes config "UTF-8")) :sensitive true}}})))
(defn temporary [] (str (Files/createTempDirectory "managed-test-" (make-array java.nio.file.attribute.FileAttribute 0))))
(defn factory [storage] (fn [opts env & [read]] (coordinator/coordinator opts env (or read (:read storage)) (:write storage) nil {:event-prefix "managed/" :reducer reducer/managed-coordination})))
(deftest deterministic-managed-plans-with-no-local-key-or-access-material
  (doseq [provider ["vultr" "digitalocean"]]
    (let [dir (temporary) opts (merge opts {:workdir dir :provider-compute provider :digitalocean-region "ams3" :doks-version "1.35.1-do.1" :digitalocean-node-size "s-2vcpu-4gb" :digitalocean-node-count 2})
          plan (managed/plan-managed-kubernetes opts)]
      (is (= "planned" (:status plan))) (is (= 2 (count (:documents plan))))
      (is (contains? (:documents plan) "backend.tf.json"))
      (is (empty? (.listFiles (io/file dir))))))
  (doseq [patch [{:vultr-node-count true} {:vultr-node-count -1} {:vultr-node-count 1.2} {:provider-compute "aws"} {:compute-prevent-destroy "false"} {:vultr-vke-version "latest"}]]
    (is (seq (managed/managed-errors (merge opts {:workdir (temporary)} patch))))))
(deftest decoder-rejects-command-file-reference-endpoint-and-credential-echo
  (doseq [content [(str/replace config "token: private-token" "exec: {command: sh}")
                   (str/replace config "token: private-token" "tokenFile: /etc/token")
                   (str/replace config "192.0.2.1" "192.0.2.2")
                   (str/replace config "server: https://192.0.2.1" "server: https://192.0.2.1, insecure-skip-tls-verify: true")
                   (str/replace config "private-token" "bound-provider-token")
                   (str/replace config "current-context: active" "current-context: missing")
                   (str/replace config "user: operator" "user: missing")
                   (str/replace config "token: private-token" "")
                   (str/replace config "server: https://192.0.2.1" "server: https://192.0.2.1, proxy-url: http://localhost:1234")
                   (str/replace config "server: https://192.0.2.1" "server: https://192.0.2.1, tls-server-name: other.example")]]
    (let [dir (temporary) decoder (access/access-decoder (assoc opts :workdir dir) {"COLORS_PAR_VULTR_API_KEY" "bound-provider-token"})]
      (is (thrown? Exception ((:decode decoder) (state-text content))))
      (is (empty? (.listFiles (io/file dir)))))))
(deftest create-and-delete-are-real-cas-transitions-with-private-access-file
  (let [dir (temporary) opts (assoc opts :workdir dir) storage (ct/store) exists (atom false) calls (atom [])
        deps {:coordinator (factory storage) :version-preflight (fn [& _] true)
              :state-presence (fn [& _] {:status (if @exists "present" "absent")})
              :read-managed-state (fn [& _] {:status "present" :params params})
              :converge-state (fn [_ _ documents operation _ _ decoder]
                                (is (not (contains? documents "backend.tf.json")))
                                (is (= "held" (get-in @(:state storage) [:observation :document :lock :state])))
                                (is (= "running" (get-in @(:state storage) [:observation :document :shared :phase])))
                                (swap! calls conj operation) (reset! exists (= operation "create"))
                                (if (= operation "delete") {:status "destroyed"}
                                  {:status "ready" :params params :outputs ((:decode decoder) (state-text))}))}
        result (managed/managed-kubernetes opts {} {"COLORS_PAR_VULTR_API_KEY" "fixture"} deps)]
    (is (= "ready" (:status result)))
    (is (not (str/includes? (json/generate-string result) "private-token")))
    (when (:kubeconfig_path result)
      (is (= config (slurp (:kubeconfig_path result))))
      (is (= "rw-------" (java.nio.file.attribute.PosixFilePermissions/toString (Files/getPosixFilePermissions (.toPath (io/file (:kubeconfig_path result))) (make-array LinkOption 0))))))
    (is (= "idle" (get-in @(:state storage) [:observation :document :lock :state])))
    (is (= {:status "destroyed"} (managed/managed-kubernetes (assoc opts :green/event :delete :compute-prevent-destroy false) {} {"COLORS_PAR_VULTR_API_KEY" "fixture"} deps)))
    (is (= "retired" (get-in @(:state storage) [:observation :document :status])))
    (is (= ["create" "delete"] @calls))))
(deftest legacy-state-refuses-before-credentials-version-or-executor
  (let [dir (temporary) storage (ct/store) calls (atom [])]
    (is (= {:status "error"} (managed/managed-kubernetes (assoc opts :workdir dir) {:legacy_state_keys ["demo/legacy.tfstate"]} {}
       {:coordinator (factory storage) :state-presence (fn [_ key & _] (swap! calls conj key) {:status "present"})
        :version-preflight (fn [& _] (throw (Exception. "must not run"))) :converge-state (fn [& _] (throw (Exception. "must not run")))})))
    (is (= ["demo/legacy.tfstate"] @calls)) (is (empty? (.listFiles (io/file dir))))))
(deftest private-reader-and-executor-accept-only-reviewed-sensitive-output
  (let [opts (assoc opts :workdir (temporary)) decoder (access/access-decoder opts {}) calls (atom [])
        responses (atom ["" (state-text)]) runner (fn [argv & _] (swap! calls conj argv) (let [out (first @responses)] (swap! responses subvec 1) {:exit 0 :out out}))
        result (runtime/read-state opts "demo/compute/managed-kubernetes.tfstate" {} runner true (:decode decoder))]
    (is (= "present" (:status result))) (is (= {:params params} (:outputs result)))
    (is (not (str/includes? (json/generate-string result) "private-token")))))
