(ns io.github.getcolors.compute-request
  "Pure provider-neutral requests resolved through packaged declarative recipes."
  (:require [io.github.getcolors.compute-options :as options] [cheshire.core :as json]
            [clojure.java.io :as io]
            [clojure.string :as str]
            [io.github.getcolors.compute :as compute])
  (:import [java.security MessageDigest]
           [java.math BigInteger]
           [java.net InetAddress]))

(def ^:private recipes (json/parse-string (slurp (io/resource "colors_compute/provider-recipes.json")) true))
(def ^:private templates (json/parse-string (slurp (io/resource "colors_compute/templates.json")) true))
(defn- fail [message] (throw (ex-info message {})))
(defn- safe? [value] (and (string? value) (boolean (re-matches #"[a-zA-Z0-9][a-zA-Z0-9_-]{0,62}" value))))
(defn- missing? [value] (or (nil? value) (and (string? value) (or (str/blank? value) (= "REPLACE_ME" (str/upper-case (str/trim value)))))))
(defn- fields? [value required optional]
  (and (map? value) (every? #(contains? value %) required)
       (every? (into required optional) (keys value))))
(defn- integer-in? [value low high]
  (and (number? value) (<= low value high) (== value (Math/floor (double value)))))
(defn- ipv4 [text]
  (when (and (string? text) (re-matches #"(0|[1-9][0-9]{0,2})(\.(0|[1-9][0-9]{0,2})){3}" text))
    (let [parts (mapv #(Long/parseLong %) (str/split text #"\."))]
      (when (every? #(<= % 255) parts) (reduce #(+ (* %1 256) %2) 0 parts)))))
(defn- ipv6-text [bytes]
  (let [parts (mapv (fn [[a b]] (bit-or (bit-shift-left (bit-and 255 a) 8) (bit-and 255 b))) (partition 2 bytes))
        runs (for [i (range 8) :when (and (zero? (parts i)) (or (zero? i) (pos? (parts (dec i)))))]
               [i (count (take-while zero? (drop i parts)))])
        [start length] (first (sort-by (fn [[i n]] [(- n) i]) runs))
        hex #(str/join ":" (map (fn [n] (Integer/toHexString n)) %))]
    (if (and length (> length 1)) (str (hex (take start parts)) "::" (hex (drop (+ start length) parts))) (hex parts))))
(defn ^:no-doc cidr
  ([value] (cidr value false))
  ([value allow-ipv6]
  (when-not (string? value) (fail "invalid compute network CIDR"))
  (let [parts (str/split value #"/" -1) address (first parts) v4 (ipv4 address)]
    (when (> (count parts) 2) (fail "invalid compute network CIDR"))
    (if (nil? v4)
      (do
        (when-not (and (str/includes? address ":") (re-matches #"[0-9a-fA-F:.]+" address))
          (fail "invalid compute network CIDR"))
        (let [bytes (try (let [^InetAddress parsed-address (InetAddress/getByName address)] (.getAddress parsed-address)) (catch Exception _ nil))
              prefix (if (= 1 (count parts)) 128
                       (when (re-matches #"[0-9]+" (second parts))
                         (try (Long/parseLong (second parts)) (catch Exception _ nil))))]
          (when-not (and bytes (= 16 (count bytes)) prefix (<= 0 prefix 128)) (fail "invalid compute network CIDR"))
          (let [number (BigInteger. 1 bytes) mask (.subtract (.shiftLeft BigInteger/ONE (int (- 128 prefix))) BigInteger/ONE)]
            (when-not (= BigInteger/ZERO (.and number mask)) (fail "invalid compute network CIDR")))
          (let [canonical (ipv6-text bytes)]
            (when-not (and allow-ipv6 (= value (str canonical "/" prefix))) (fail "unsupported compute network address family"))
            {:address canonical :prefix prefix :version 6})))
      (let [prefix (if (= 1 (count parts)) 32
                     (let [part (second parts)]
                       (if (re-matches #"[0-9]+" part)
                         (try (Long/parseLong part) (catch Exception _ nil))
                         (when-let [mask (ipv4 part)]
                           (let [binary (str/replace (format "%32s" (Long/toBinaryString mask)) " " "0")]
                             (cond (re-matches #"1*0*" binary) (count (take-while #{\1} binary))
                                   (re-matches #"0*1*" binary) (count (take-while #{\0} binary))))))))]
        (when-not (and prefix (<= 0 prefix 32)) (fail "invalid compute network CIDR"))
        (let [size (bit-shift-left 1 (int (- 32 prefix)))]
          (when-not (zero? (mod v4 size)) (fail "invalid compute network CIDR"))
          (when-not (= value (str address "/" prefix)) (fail "unsupported compute network address family"))
          {:address address :prefix prefix :start v4 :end (+ v4 size -1) :version 4}))))))
(defn- binding-value [spec context]
  (cond
    (contains? spec :value) (:value spec)
    (contains? spec :path)
    (let [value (reduce #(when (map? %1) (get %1 (keyword %2))) context (str/split (:path spec) #"\."))]
      (if (missing? value)
        (if (contains? spec :default) (:default spec) (fail (str "missing compute input: " (:path spec))))
        value))
    (contains? spec :first)
    (let [resolved (some (fn [candidate]
                          (try {:value (binding-value candidate context)}
                               (catch Exception error
                                 (when-not (str/starts-with? (.getMessage error) "missing compute input: ") (throw error)))))
                        (:first spec))]
      (if resolved (:value resolved) (fail (str "missing compute input: " (:path (first (:first spec)))))))
    (contains? spec :list) (mapv #(binding-value % context) (:list spec))
    (contains? spec :object) (into {} (map (fn [[key item]] [key (binding-value item context)]) (:object spec)))
    :else (fail "invalid provider recipe")))
(defn- hash-suffix [value]
  (subs (format "%064x" (BigInteger. 1 (.digest (MessageDigest/getInstance "SHA-256") (.getBytes value "UTF-8")))) 0 12))
(defn- rules [format-name request network name]
  (let [expanded (vec (mapcat (fn [rule]
                               (if (contains? rule :peer_roles)
                                 (for [[id peer] (sort-by (comp clojure.core/name clojure.core/key) (:peers request))
                                       :when (some #{(:role peer)} (:peer_roles rule))]
                                   [(str (:id rule) ":peer:" (clojure.core/name id)) rule (str (:vpc_ip peer) "/32") false])
                                 (for [source (sort (:sources rule))]
                                   (let [private? (= "private" source)
                                         range (if (and private? (= "digitalocean" format-name)) nil (if private? network source))]
                                     (when (and private? (not= "digitalocean" format-name) (nil? range)) (fail "missing compute network CIDR for private ingress"))
                                     [(str (:id rule) ":" source) rule range private?]))))
                             (sort-by :id (get-in request [:security :ingress]))))
        {:keys [result public private public-rules]}
        (reduce-kv
         (fn [acc ordinal [key rule range private?]]
           (let [protocol (:protocol rule) icmp? (= protocol "icmp")
                 lo (when-not icmp? (long (:from_port rule))) hi (when-not icmp? (long (:to_port rule)))
                 ports (when-not icmp? (if (= lo hi) (str lo) (str lo "-" hi)))]
             (case format-name
               "vultr" (let [net (cidr range true)] (assoc-in acc [:result key] {:protocol protocol :port ports :ip_type (str "v" (:version net)) :subnet (:address net) :subnet_size (:prefix net)}))
               "aws" (assoc-in acc [:result key] {:protocol protocol :from_port (if icmp? -1 lo) :to_port (if icmp? -1 hi) :cidr range})
               "azure" (do (when (>= ordinal 3996) (fail "too many compute ingress rules"))
                           (assoc-in acc [:result (str/replace key #"[:/.]" "-")] {:priority (+ 100 ordinal) :protocol (str/capitalize protocol) :port (if icmp? "*" ports) :sources [range]}))
               "google" (assoc-in acc [:result key] {:name (str name "-" (hash-suffix key)) :protocol protocol :ports (if icmp? [] [ports]) :source_ranges [range]})
               "yandex" (assoc-in acc [:result key] {:protocol (str/upper-case protocol) :from_port lo :to_port hi :cidr_blocks [range]})
               "digitalocean" (assoc-in acc [(if (nil? range) :private :public) key]
                                       (cond-> {:protocol protocol} (not icmp?) (assoc :port_range ports) (some? range) (assoc :source_addresses [range])))
               "hcloud" (update acc :public-rules conj (cond-> {:direction "in" :protocol protocol :source_ips [range]} (not icmp?) (assoc :port ports)))
               "oci" (assoc-in acc [:result key] {:direction "INGRESS" :protocol (if icmp? "1" (if (= protocol "tcp") "6" "17")) :cidr range :from_port lo :to_port hi})
               (fail "unsupported compute firewall format"))))
         {:result {} :public {} :private {} :public-rules []} expanded)
        result (cond-> result (= format-name "oci") (assoc "outbound-all" {:direction "EGRESS" :protocol "all" :cidr "0.0.0.0/0" :from_port nil :to_port nil}))
        egress (if (= format-name "yandex")
                 {"all-ipv4" {:protocol "ANY" :from_port 0 :to_port 65535 :cidr_blocks ["0.0.0.0/0"]}}
                 {"all-ipv4" {:protocol "-1" :from_port nil :to_port nil :cidr "0.0.0.0/0"}})]
    {:ingress result :rules result :egress egress :public_ingress public :private_ingress private :public_rules public-rules
     :outbound_rules (mapv #(cond-> {:protocol % :destination_addresses ["0.0.0.0/0" "::/0"]}
                             (not= % "icmp") (assoc :port_range "1-65535")) ["tcp" "udp" "icmp"])}))
(defn- check-literals! [value]
  (cond
    (keyword? value) (check-literals! (subs (str value) 1))
    (string? value) (when (or (str/includes? value (str "$" "{")) (str/includes? value "%{")) (fail "invalid compute literal"))
    (map? value) (doseq [[key item] value] (check-literals! key) (check-literals! item))
    (sequential? value) (doseq [item value] (check-literals! item))))

(declare provider-request)
(defn- provider-request-resolved
  ([opts stage request] (provider-request-resolved opts stage request {}))
  ([opts stage request shared]
   (let [provider (:provider-compute opts) recipe (when (string? provider) (get recipes (keyword provider)))]
     (when-not recipe (fail "compute provider recipe unavailable"))
     (when (contains? request :endpoint)
       (when-not (= {:kind "reserved-ip" :assignment "application"} (:endpoint request)) (fail "invalid compute endpoint request"))
       (when-not (:application_reserved_ip recipe) (fail "unsupported compute endpoint capability")))
     (when-not (contains? #{"shared" "node"} stage) (fail "unsupported compute request stage"))
     (when-not (safe? (:profile opts)) (fail "invalid compute profile"))
     (when-not (and (fields? request #{:node_id :key :network :security} #{:name :endpoint :role :roles :peers :backups :ipv6}) (safe? (:node_id request)))
       (fail "invalid compute request"))
     (let [roles (:roles request) peers (get request :peers {})]
       (when (and (some? roles) (not (and (:role_firewalls recipe) (map? roles) (seq roles)
              (every? (fn [[role policy]] (and (safe? (clojure.core/name role)) (fields? policy #{:security} #{}))) roles))))
         (fail "unsupported compute role firewall policy"))
       (when-not (and (map? peers) (<= (count peers) 1000)) (fail "invalid compute peers"))
       (doseq [[id peer] peers]
         (when-not (and (safe? (clojure.core/name id)) (fields? peer #{:role :vpc_ip} #{}) (some? roles)
                        (string? (:role peer)) (contains? roles (keyword (:role peer))) (ipv4 (:vpc_ip peer)))
           (fail "invalid compute peers"))
         (when-not (re-matches (re-pattern (str (java.util.regex.Pattern/quote (:role peer)) "-(0|[1-9][0-9]{0,2})")) (clojure.core/name id))
           (fail "invalid compute peer identity"))))
     (let [profile (:profile opts) name (get request :name (if (= "shared" stage) profile (str profile "-" (:node_id request))))
           {:keys [key network security]} request
           entry (get-in compute/registry [:compute (keyword provider)])
           registration-owned (or (= "managed" (:mode key)) (:registration_external recipe))]
       (when-not (safe? name) (fail "invalid compute name"))
       (when-not (and (fields? key #{:mode} #{:public_key :ids :reference}) (contains? #{"managed" "external"} (:mode key)))
         (fail "invalid compute key request"))
       (when-not (fields? network #{:mode} #{:cidr :subnet_cidr :zone :private_ip :id}) (fail "invalid compute network request"))
       (when-not (some #{(:mode network)} (or (:network_modes recipe) [(:network_mode recipe)])) (fail "unsupported compute network mode"))
       (when (contains? network :id)
         (when-not (and (= (:mode network) (get-in recipe [:network_reference :mode])) (string? (:id network))
                        (some? (get-in recipe [:network_reference :pattern]))
                        (re-matches (re-pattern (get-in recipe [:network_reference :pattern])) (:id network)))
           (fail "invalid compute network reference")))
       (when-not (and (fields? security #{:ingress :egress :private_filter} #{}) (= "all" (:egress security)) (boolean? (:private_filter security)))
         (fail "unsupported compute security policy"))
       (when (and (= "none" (:mode network)) (or (not= #{:mode} (set (keys network))) (:private_filter security) (some? (:roles request))
          (some #(and (map? %) (or (contains? % :peer_roles) (some #{"private"} (:sources %)))) (:ingress security)))) (fail "network none requires public-only security"))
       (when (and (:private_filter security) (not (:private_filter recipe))) (fail "unsupported compute private filtering"))
       (when-not (and (vector? (:ingress security)) (seq (:ingress security))) (fail "invalid compute ingress"))
       (let [has-ssh (atom false)]
         (reduce
          (fn [seen rule]
            (when-not (and (fields? rule #{:id :protocol :from_port :to_port} #{:sources :peer_roles})
                           (not= (contains? rule :sources) (contains? rule :peer_roles)) (safe? (:id rule)) (not (contains? seen (:id rule))))
              (fail "invalid compute ingress"))
            (when-not (or (and (= "icmp" (:protocol rule)) (nil? (:from_port rule)) (nil? (:to_port rule)))
                          (and (contains? #{"tcp" "udp"} (:protocol rule))
                           (integer-in? (:from_port rule) 1 65535) (integer-in? (:to_port rule) (:from_port rule) 65535)))
              (fail "invalid compute ingress"))
            (if (contains? rule :peer_roles)
              (when-not (and (:role_firewalls recipe) (map? (:roles request))
                            (vector? (:peer_roles rule)) (seq (:peer_roles rule))
                            (every? #(and (string? %) (contains? (:roles request) (keyword %))) (:peer_roles rule))
                            (= (count (:peer_roles rule)) (count (set (:peer_roles rule))))) (fail "invalid compute peer roles"))
              (do
            (when-not (and (vector? (:sources rule)) (seq (:sources rule)) (every? string? (:sources rule))
                           (= (count (:sources rule)) (count (set (:sources rule)))))
              (fail "invalid compute ingress"))
            (doseq [source (:sources rule)]
              (if (= source "private")
                (when (= "hcloud" (:firewall_format recipe)) (fail "unsupported compute private filtering"))
                (do (cidr source (:ipv6_ingress recipe)) (when (and (= "tcp" (:protocol rule)) (<= (:from_port rule) 22 (:to_port rule))) (reset! has-ssh true)))))
              ))
            (conj seen (:id rule))) #{} (:ingress security))
         (when-not @has-ssh (fail "compute public SSH ingress required")))
       (let [network-cidr (if (missing? (:cidr network))
                            (some #(let [value (get opts (keyword %))] (when-not (missing? value) value)) (:network_cidr_options recipe))
                            (:cidr network))
             subnet (if (missing? (:subnet_cidr network))
                      (or (some #(let [value (get opts (keyword %))] (when-not (missing? value) value)) (:subnet_cidr_options recipe)) network-cidr)
                      (:subnet_cidr network))
             parsed (when (some? network-cidr) (cidr network-cidr))
             parsed-subnet (when (some? subnet) (cidr subnet))
             shared (if (nil? shared) {} shared)]
         (when (and parsed (some (fn [peer] (not (< (:start parsed) (ipv4 (:vpc_ip peer)) (:end parsed)))) (vals (:peers request))))
           (fail "compute peer outside private network"))
         (when (and parsed parsed-subnet (not (<= (:start parsed) (:start parsed-subnet) (:end parsed-subnet) (:end parsed))))
           (fail "compute subnet must be inside network"))
         (when-not (missing? (:private_ip network))
           (when-not (:static_private_ip recipe) (fail "unsupported compute static private address"))
           (let [address (ipv4 (:private_ip network))]
             (when-not (and address parsed-subnet (<= (:start parsed-subnet) address (:end parsed-subnet)))
               (fail "invalid compute private address"))))
         (when-not (boolean? (get opts :compute-prevent-destroy true)) (fail "invalid compute prevent-destroy flag"))
         (when-not (map? shared) (fail "invalid compute shared results"))
         (when (and (= stage "node") (not (and (map? (:params shared)) (= provider (get-in shared [:params :provider])))))
           (fail "compute shared provider mismatch"))
         (when (and (= stage "node") (some? (:roles request)))
           (when-not (and (string? (:role request)) (contains? (:roles request) (keyword (:role request)))
                          (map? (get-in shared [:params :role_firewall_ids]))
                          (contains? (get-in shared [:params :role_firewall_ids]) (keyword (:role request))))
             (fail "missing compute role firewall")))
         (let [shared (if (and (= stage "node") (some? (:roles request)))
                         (cond-> (assoc-in shared [:params (keyword (:role_firewall_param recipe))] (get-in shared [:params :role_firewall_ids (keyword (:role request))]))
                           (:role_tag_param recipe) (assoc-in [:params (keyword (:role_tag_param recipe))]
                             (or (get-in shared [:params :role_tags (keyword (:role request))]) (fail "missing compute role tag")))) shared)
               primary (if registration-owned (:ssh_key_id shared) (:reference key))
               ids (if (and registration-owned (some? primary)) [primary] (get key :ids []))]
           (when-not (and (vector? ids) (every? #(or (and (string? %) (not (missing? %))) (integer-in? % 1 9007199254740991)) ids))
             (fail "invalid compute key references"))
           (when (and (= stage "node") (:registration entry) (empty? ids)) (fail "compute key references required"))
           (let [derived (merge {:endpoint_count (if (contains? request :endpoint) 1 0) :profile profile :name name :node_id (:node_id request) :prevent_destroy (get opts :compute-prevent-destroy true)
                                 :public_key (:public_key key) :ssh_key_id primary :ssh_key_ids ids :key_name (get shared :key_name (:reference key))
                                 :user (:user entry) :sudoer (:sudoer entry) :network_cidr network-cidr :subnet_cidr subnet
                                 :network_address (:address parsed) :network_prefix (:prefix parsed)
                                 :network_name (str profile "-network") :subnet_name (str profile "-subnet") :firewall_name (str profile "-firewall")
                                 :network_tag (str profile "-network") :deployment_tag (str "colors-compute-" profile)
                                 :nic_name (str name "-nic") :public_ip_name (str name "-public")}
                                (rules (:firewall_format recipe) request network-cidr profile))
                 image (if (and (missing? (:google-image-id opts)) (not (missing? (:google-image-project opts))) (not (missing? (:google-image-family opts))))
                         (str "projects/" (:google-image-project opts) "/global/images/family/" (:google-image-family opts))
                         (:google-image-id opts))
                 derived (assoc derived :google_image image)
                 selected-stage (cond
                                  (and (= stage "shared") registration-owned (:registration entry)) "shared-keygen"
                                  (and (= stage "node") (:discovery_stage recipe) (missing? (get opts (keyword (:image_option recipe))))) (:discovery_stage recipe)
                                  :else stage)
                 _ (when (and (= "created" (:mode network)) (:network_stages recipe) (nil? network-cidr)) (fail "compute created network CIDR required"))
                 selected-stage (get-in recipe [:network_stages (keyword (:mode network)) (keyword selected-stage)] selected-stage)
                 selected-stage (if (contains? network :id) (get-in recipe [:network_reference :stages (keyword selected-stage)] selected-stage) selected-stage)
                 role-derived
                 (when (and (= stage "shared") (some? (:roles request)))
                   (let [roles (:roles request)
                         rendered (into {} (map (fn [[role policy]]
                           (let [role-name (clojure.core/name role) role-request (assoc request :role role-name :security (:security policy))
                                 validation-shared (assoc (merge (:planning_shared recipe) shared) :ssh_key_id (or primary "validation-key")
                                   :params (merge (get-in recipe [:planning_shared :params]) {:provider provider :vpc_id "validation-vpc"
                                            :role_firewall_ids (zipmap (keys roles) (repeat "validation-firewall"))
                                            :role_tags (zipmap (keys roles) (repeat "validation-tag"))}))]
                             (provider-request opts "node" role-request validation-shared)
                             [role (rules (:firewall_format recipe) role-request network-cidr profile)])) roles))]
                     {:role_ingress (into {} (mapcat (fn [[role value]] (map (fn [[id rule]] [(str (clojure.core/name role) ":" id) (assoc rule :role (clojure.core/name role))]) (:ingress value))) rendered))
                      :role_public_ingress (into {} (map (fn [[role value]] [role (:public_ingress value)]) rendered))
                      :role_private_ingress (into {} (map (fn [[role value]] [role (:private_ingress value)]) rendered))
                      :role_tags (into {} (map (fn [role] [role (str "colors-compute-" profile "-" (clojure.core/name role))]) (keys roles)))
                      :firewall_groups (into {} (map (fn [role] [role (str profile "-" (clojure.core/name role) "-firewall")]) (keys roles)))}))
                 derived (merge derived role-derived)
                 selected-stage (if role-derived (get-in recipe [:role_stages (keyword selected-stage)]) selected-stage)
                 selected-stage (reduce (fn [selected [option choices]]
                    (if (contains? opts option)
                     (do (when-not (boolean? (get opts option)) (fail "invalid compute boolean option"))
                         (get-in choices [(keyword (str (get opts option))) (keyword selected)] selected)) selected)) selected-stage (:boolean_stages recipe))
                 documents (get-in templates [(keyword provider) (keyword selected-stage)])
                 tokens (sort (set (map second (re-seq #"\{\{([a-z_]+)\}\}" (json/generate-string documents)))))
                 context {:opts opts :request request :shared shared :derived derived}
                 inputs (into {} (for [token tokens]
                                   (let [binding (get-in recipe [:bindings (keyword token)])]
                                     (when-not binding (fail (str "missing provider recipe binding: " token)))
                                     [(keyword token) (if (= token "ssh_key_id") primary (binding-value binding context))])))]
             (check-literals! inputs)
             {:provider provider :stage selected-stage :inputs inputs :documents (options/apply-options provider stage request (compute/provider-plan provider selected-stage inputs) opts)})))))))

(defn provider-request
  ([opts stage request] (provider-request opts stage request {}))
  ([opts stage request shared]
   (let [provider (:provider-compute opts) recipe (when (string? provider) (get recipes (keyword provider)))
         role (:role request)]
     (when-not recipe (fail "compute provider recipe unavailable"))
     (let [opts
           (if (nil? role) opts
             (do
               (when-not (safe? role) (fail "invalid compute role"))
               (let [settings (get opts :compute-role-settings {}) values (get settings (keyword role) {})]
                 (when-not (and (map? settings) (fields? values #{} #{:size :image})) (fail "invalid compute role settings"))
                 (let [opts (reduce-kv (fn [opts kind value]
                                        (let [target (get-in recipe [:role_options kind])]
                                          (when-not target (fail "unsupported compute role setting"))
                                          (when (missing? value) (fail "invalid compute role settings"))
                                          (assoc opts (keyword target) value))) opts values)]
                   (if (contains? values :size) opts
                       (if-let [candidate (some #(let [v (get opts (keyword (str/replace % "{role}" role)))] (when-not (missing? v) v)) (:role_size_legacy recipe))]
                         (assoc opts (keyword (get-in recipe [:role_options :size])) candidate) opts))))))]
       (provider-request-resolved opts stage request shared)))))
