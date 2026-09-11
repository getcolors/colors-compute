(ns io.github.getcolors.compute-inspection
  "Read the recorded deployment inventory without changing journal ownership."
  (:require [io.github.getcolors.compute :as compute]
            [io.github.getcolors.compute-runtime :as runtime]
            [io.github.getcolors.compute-journal :as journal]
            [io.github.getcolors.compute-ssh :as ssh]
            [io.github.getcolors.compute-lifecycle :as lifecycle]))
(defn- require-valid [value] (when-not value (throw (ex-info "compute inspection failed" {}))))
(defn read-deployment
  ([opts] (read-deployment opts (into {} (System/getenv)) {}))
  ([opts environment] (read-deployment opts environment {}))
  ([opts environment dependencies] (read-deployment opts environment dependencies {}))
  ([opts environment dependencies requirements]
   (try
     (require-valid (map? requirements))
     (let [observed ((get dependencies :journal-get journal/journal-get) opts environment)]
       (if (= observed {:status "absent"}) {:status "absent"}
           (let [doc (:document observed)
                 config (compute/backend-settings opts (str (:profile opts) "/compute/coordination.json"))
                 expected {:profile (:profile opts) :provider (:provider-compute opts)
                           :backend (cond-> {:kind (:provider-backend opts) :bucket (:bucket config) :region (:region config)}
                                      (contains? #{"r2" "oci"} (:provider-backend opts)) (assoc :endpoint (get-in config [:endpoints :s3])))}]
             (require-valid (and (= "present" (:status observed)) (lifecycle/valid-document? doc)
                                 (= expected (:identity doc))
                                 (= "idle" (get-in doc [:lock :state]))))
             (if (= "retired" (:status doc)) {:status "destroyed"}
                 (let [shared ((get dependencies :read-state (fn [opts key env] (runtime/read-state opts key env runtime/run-command true))) opts (:shared (compute/state-keys (:profile opts) [])) environment)
                       _ (require-valid (and (= "present" (:status shared)) (= (:provider-compute opts) (get-in shared [:params :provider]))))
                       selected (ssh/mode opts)
                       _ (require-valid (= (:mode selected) (get-in doc [:key :mode])))
                       private-path (if (= "managed" (get-in doc [:key :mode])) (str (or (get environment "HOME") (System/getProperty "user.home")) "/.ssh/" (:profile opts)) (:private_key_path selected))
                       records (remove #(= "destroyed" (:phase (val %))) (:nodes doc))
                       entries (mapv (fn [[node-id node]]
                                       (require-valid (contains? #{"ready" "failed"} (:phase node)))
                                       (let [state ((get dependencies :read-state runtime/read-state) opts (:state_key node) environment)]
                                         (require-valid (= "present" (:status state)))
                                         {:request {:node_id (name node-id) :role (:role node) :index (:index node) :provider (:provider-compute opts)}
                                          :params (cond-> (:params state) private-path (assoc :ssh_identity_file private-path))})) records)
                       declarations (vec (sort-by (juxt #(or (:role %) "") :index) (map :request entries)))
                       entry (get requirements :entry_node_id (:node_id (first declarations)))]
                   (require-valid (and (seq declarations) (string? entry) (some #(= entry (:node_id %)) declarations)))
                   {:status "present" :shared (:outputs shared) :cluster (compute/collect declarations (mapv :params entries) entry)
                    :key (cond-> {:mode (get-in doc [:key :mode])}
                           private-path (assoc :private_key_path private-path))})))))
     (catch InterruptedException error (throw error))
     (catch Exception _ {:status "error"}))))
