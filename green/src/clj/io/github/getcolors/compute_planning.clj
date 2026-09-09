(ns io.github.getcolors.compute-planning
  (:require [io.github.getcolors.compute :as compute]
            [io.github.getcolors.compute-request :as request]
            [io.github.getcolors.compute-deployment-request :as deployment]
            [io.github.getcolors.compute-key-request :as key]
            [io.github.getcolors.compute-ssh :as ssh]))
(defn- address [number] (clojure.string/join "." (map #(bit-and 255 (bit-shift-right number %)) [24 16 8 0])))
(defn- planning-shared [opts recipe requirements]
  (cond-> (:planning_shared recipe)
    (contains? requirements :roles)
    (assoc-in [:params :role_firewall_ids] (into {} (map (fn [role] [role (str "build-firewall-" (name role))]) (keys (:roles requirements)))))
    (and (contains? requirements :roles) (:role_tag_param recipe))
    (assoc-in [:params :role_tags] (into {} (map (fn [role] [role (str "colors-compute-" (:profile opts) "-" (name role))]) (keys (:roles requirements)))))))
(defn plan-deployment [opts topology requirements]
  (let [opts (assoc opts :green/event :build) selected (ssh/mode opts)
        selected (cond-> selected (= "managed" (:mode selected)) (assoc :public_key ssh/placeholder-public))
        key (key/key-request opts selected {}) assembly (deployment/deployment-requests opts topology requirements key)
        provider (:provider-compute opts) recipe (get deployment/recipes (keyword provider)) shared (planning-shared opts recipe requirements)
        shared-plan (request/provider-request opts "shared" (:shared assembly)) declarations (compute/expand topology)
        _ (when (> (count declarations) 245) (throw (ex-info "build exceeds documentation address capacity" {})))
        entry (get-in compute/registry [:compute (keyword provider)])
        network (get-in assembly [:shared :network])
        cidr (or (:subnet_cidr network) (:cidr network)
                 (some #(get opts (keyword %)) (concat (:subnet_cidr_options recipe) (:network_cidr_options recipe))) "10.0.0.0/24")
        parsed (when-not (= "none" (:mode network)) (request/cidr cidr))
        shared (if parsed (assoc-in shared [:params :network_cidr] (str (:address parsed) "/" (:prefix parsed))) (update shared :params #(apply dissoc % [:vpc_id :vpc_ip_range :network_cidr :subnet_id :subnet_cidr])))
        shared (cond-> shared (contains? requirements :endpoint) (assoc-in [:params :endpoint_ip] "198.51.100.10"))
        resolved (mapv (fn [ordinal node]
                         (let [plan (request/provider-request opts "node" node shared) offset (+ ordinal 10)]
                           (when (and parsed (>= offset (- (:end parsed) (:start parsed)))) (throw (ex-info "build exceeds private network address capacity" {})))
                           {:documents (:documents plan)
                            :params (cond-> {:provider_id (if (re-matches #"[0-9]+" (:planning_provider_id recipe)) (str (+ (Long/parseLong (:planning_provider_id recipe)) ordinal)) (str (:planning_provider_id recipe) "-" (:node_id node))) :node_id (:node_id node) :provider provider :name (:name node) :ip (str "192.0.2." offset)
                                             :vpc_ip (when parsed (address (+ (:start parsed) offset))) :user (:user entry) :sudoer (:sudoer entry)}
                                      (= "managed" (:mode selected)) (assoc :ssh_identity_file (str "$HOME/.ssh/" (:profile opts)))
                                      (:private_key_path selected) (assoc :ssh_identity_file (:private_key_path selected)))}))
                       (range) (:nodes assembly))]
    {:status "planned" :shared shared :documents {:shared (:documents (if (contains? requirements :roles)
         (request/provider-request opts "shared" (assoc (:shared assembly) :peers
           (into {} (map (fn [declaration result] [(keyword (:node_id declaration)) {:role (:role declaration) :vpc_ip (get-in result [:params :vpc_ip])}]) declarations resolved)))) shared-plan)) :nodes (into {} (map (fn [node result] [(:node_id node) (:documents result)]) (:nodes assembly) resolved))}
     :state_keys (compute/state-keys (:profile opts) (mapv :node_id declarations))
     :cluster (compute/collect declarations (mapv :params resolved) (:entry_node_id assembly))
     :key (cond-> (select-keys selected [:mode :private_key_path]) (= "managed" (:mode selected)) (assoc :private_key_path (str "$HOME/.ssh/" (:profile opts))))}))

(defn validate-deployment [opts topology requirements]
  (let [opts (assoc opts :green/event :build) selected (ssh/mode opts)
        selected (cond-> selected (= "managed" (:mode selected)) (assoc :public_key ssh/placeholder-public))
        assembly (deployment/deployment-requests opts topology requirements (key/key-request opts selected {}))
        recipe (get deployment/recipes (keyword (:provider-compute opts)))]
    (request/provider-request opts "shared" (:shared assembly))
    (doseq [node (:nodes assembly)] (request/provider-request opts "node" node (planning-shared opts recipe requirements)))
    true))
