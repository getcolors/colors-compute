(ns io.github.getcolors.compute-planning
  (:require [io.github.getcolors.compute :as compute]
            [io.github.getcolors.compute-request :as request]
            [io.github.getcolors.compute-deployment-request :as deployment]
            [io.github.getcolors.compute-key-request :as key]
            [io.github.getcolors.compute-ssh :as ssh]))
(defn- address [number] (clojure.string/join "." (map #(bit-and 255 (bit-shift-right number %)) [24 16 8 0])))
(defn plan-deployment [opts topology requirements]
  (let [opts (assoc opts :green/event :build) selected (ssh/mode opts)
        selected (cond-> selected (= "managed" (:mode selected)) (assoc :public_key ssh/placeholder-public))
        key (key/key-request opts selected {}) assembly (deployment/deployment-requests opts topology requirements key)
        provider (:provider-compute opts) recipe (get deployment/recipes (keyword provider)) shared (:planning_shared recipe)
        shared-plan (request/provider-request opts "shared" (:shared assembly)) declarations (compute/expand topology)
        _ (when (> (count declarations) 245) (throw (ex-info "build exceeds documentation address capacity" {})))
        entry (get-in compute/registry [:compute (keyword provider)])
        network (get-in assembly [:shared :network])
        cidr (or (:subnet_cidr network) (:cidr network)
                 (some #(get opts (keyword %)) (concat (:subnet_cidr_options recipe) (:network_cidr_options recipe))) "10.0.0.0/24")
        parsed (request/cidr cidr)
        shared (assoc-in shared [:params :network_cidr] (str (:address parsed) "/" (:prefix parsed)))
        resolved (mapv (fn [ordinal node]
                         (let [plan (request/provider-request opts "node" node shared) offset (+ ordinal 10)]
                           (when (>= offset (- (:end parsed) (:start parsed))) (throw (ex-info "build exceeds private network address capacity" {})))
                           {:documents (:documents plan)
                            :params (cond-> {:node_id (:node_id node) :provider provider :name (:name node) :ip (str "192.0.2." offset)
                                             :vpc_ip (address (+ (:start parsed) offset)) :user (:user entry) :sudoer (:sudoer entry)}
                                      (= "managed" (:mode selected)) (assoc :ssh_identity_file (str "$HOME/.ssh/" (:profile opts)))
                                      (:private_key_path selected) (assoc :ssh_identity_file (:private_key_path selected)))}))
                       (range) (:nodes assembly))]
    {:status "planned" :shared shared :documents {:shared (:documents shared-plan) :nodes (into {} (map (fn [node result] [(:node_id node) (:documents result)]) (:nodes assembly) resolved))}
     :state_keys (compute/state-keys (:profile opts) (mapv :node_id declarations))
     :cluster (compute/collect declarations (mapv :params resolved) (:node_id (first declarations)))
     :key (cond-> (select-keys selected [:mode :private_key_path]) (= "managed" (:mode selected)) (assoc :private_key_path (str "$HOME/.ssh/" (:profile opts))))}))

(defn validate-deployment [opts topology requirements]
  (let [opts (assoc opts :green/event :build) selected (ssh/mode opts)
        selected (cond-> selected (= "managed" (:mode selected)) (assoc :public_key ssh/placeholder-public))
        assembly (deployment/deployment-requests opts topology requirements (key/key-request opts selected {}))
        recipe (get deployment/recipes (keyword (:provider-compute opts)))]
    (request/provider-request opts "shared" (:shared assembly))
    (doseq [node (:nodes assembly)] (request/provider-request opts "node" node (:planning_shared recipe)))
    true))
