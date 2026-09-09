(ns io.github.getcolors.compute-power
  "Serialized power operations on an owned singleton."
  (:require [cheshire.core :as json] [clojure.java.io :as io] [clojure.string :as str]
            [io.github.getcolors.compute :as compute]
            [io.github.getcolors.compute-coordinator :as coordinator]
            [io.github.getcolors.compute-journal :as journal]
            [io.github.getcolors.compute-lifecycle :as lifecycle]
            [io.github.getcolors.compute-runtime :as runtime]
            [io.github.getcolors.compute-ssh :as ssh])
  (:import [java.net URL Proxy HttpURLConnection InetAddress]
           [java.io ByteArrayOutputStream]
           [java.nio.file Files] [java.nio ByteBuffer] [java.nio.charset StandardCharsets]))
(def descriptors (json/parse-string (slurp (io/resource "colors_compute/power-providers.json")) true))
(defn- require-valid [v] (when-not v (throw (ex-info "compute power refused" {}))))
(defn- parse-json [text]
  (require-valid (and (string? text) (<= (alength (.getBytes ^String text StandardCharsets/UTF_8)) 2097152)))
  (journal/parse-one text))
(defn- safe-output [value env]
  (let [serialized (json/generate-string value) names (set (for [section [:compute :backend] entry (vals (get compute/registry section)) key (:secrets entry)] (str "COLORS_PAR_" (str/replace (str/upper-case key) "-" "_"))))]
    (doseq [[key secret] env :when (and (contains? names key) (string? secret) (seq secret))]
      (let [escaped (json/generate-string secret)]
        (require-valid (not (or (str/includes? serialized secret) (str/includes? serialized (subs escaped 1 (dec (count escaped)))))))))))
(defn- ip [value]
  (require-valid (and (string? value) (re-matches #"(?:0|[1-9][0-9]{0,2})(?:\.(?:0|[1-9][0-9]{0,2})){3}" value)
                      (every? #(<= 0 (Long/parseLong %) 255) (str/split value #"\.")))) value)
(defn- native-http [method url headers]
  (let [connection ^HttpURLConnection (.openConnection (URL. url) Proxy/NO_PROXY)]
    (try
      (.setRequestMethod connection method) (.setInstanceFollowRedirects connection false)
      (.setConnectTimeout connection 30000) (.setReadTimeout connection 30000)
      (doseq [[key value] headers] (.setRequestProperty connection key value))
      (when (= method "POST")
        (.setDoOutput connection true) (.setFixedLengthStreamingMode connection (int 0))
        (with-open [stream (.getOutputStream connection)] (.flush stream)))
      (require-valid (<= 200 (.getResponseCode connection) 299))
      (with-open [stream (.getInputStream connection) output (ByteArrayOutputStream.)]
        (let [deadline (+ (System/nanoTime) 30000000000) buffer (byte-array 65536)]
          (loop []
            (let [remaining (long (/ (- deadline (System/nanoTime)) 1000000))]
              (require-valid (pos? remaining)) (.setReadTimeout connection (int (max 1 remaining)))
              (let [size (.read stream buffer)]
                (when (pos? size) (.write output buffer 0 size) (require-valid (<= (.size output) 2097152)) (recur)))))
          (str (.decode (.newDecoder StandardCharsets/UTF_8) (ByteBuffer/wrap (.toByteArray output))))))
      (finally (.disconnect connection)))))
(defn- internal-provider-power
  ([opts action provider-id environment] (internal-provider-power opts action provider-id environment {}))
  ([opts action provider-id environment dependencies]
   (let [descriptor (get descriptors (keyword (:provider-compute opts))) target (get-in descriptor [:actions (keyword action)])
         _ (require-valid target)
         _ (require-valid (and (string? provider-id) (re-matches (re-pattern (:id_pattern descriptor)) provider-id)))
         wait (get opts :power-wait-seconds 300)
         _ (require-valid (and (number? wait) (== wait (Math/floor (double wait))) (<= 1 wait 1800)))
         call (fn [key fallback & args] (apply (get dependencies key fallback) args))
         result
         (if (= "oci" (:transport descriptor))
           (let [profile (:oci-config-file-profile opts)
                 _ (require-valid (and (string? profile) (re-matches #"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}" profile)))
                 env (into {} (filter (fn [[k _]] (or (= k "OCI_CLI_AUTH") (not (some #(str/starts-with? k %) ["TF_" "TOFU_" "OCI_CLI_"])))) environment))
                 prefix ["oci" "--config-file" (str (io/file (or (get environment "HOME") (System/getProperty "user.home")) ".oci/config")) "--profile" profile
                         "--cli-rc-file" "/dev/null" "--no-retry" "--output" "json"]
                 command (fn [args timeout]
                           (let [_ (safe-output (into prefix args) environment) directory (Files/createTempDirectory "colors-power-" (make-array java.nio.file.attribute.FileAttribute 0))]
                             (try
                               (let [response (call :runner runtime/run-command
                                                    (into prefix args) (str directory) env timeout)]
                                 (require-valid (and (number? (:exit response)) (zero? (:exit response))))
                                 (parse-json (:out response)))
                               (finally (Files/deleteIfExists directory)))))
                 args ["compute" "instance" "get" "--instance-id" provider-id]
                 instance (:data (command args 30000))
                 _ (require-valid (and (= provider-id (:id instance)) (string? (:lifecycle-state instance))))
                 instance (if (= (:state target) (:lifecycle-state instance)) instance
                              (do (command ["compute" "instance" "action" "--instance-id" provider-id "--action" (:action target)
                                            "--wait-for-state" (:state target) "--max-wait-seconds" (str (long wait))] (+ 30000 (* 1000 wait)))
                                  (:data (command args 30000))))]
             (require-valid (and (= provider-id (:id instance)) (= (:state target) (:lifecycle-state instance))))
             (if (= action "start")
               (let [vnics (:data (command ["compute" "instance" "list-vnics" "--instance-id" provider-id] 30000))
                     _ (require-valid (vector? vnics)) addresses (keep :public-ip vnics)]
                 (require-valid (= 1 (count addresses))) {:status "ready" :ip (ip (first addresses))})
               {:status "ready"}))
           (let [token (get environment (:credential descriptor))
                 _ (require-valid (and (string? token) (not (str/blank? token)) (not= "REPLACE_ME" (str/upper-case (str/trim token))) (not (re-find #"[\r\n]" token))))
                 headers {"Authorization" (str "Bearer " token) "Accept" "application/json"}
                 url (str (:origin descriptor) "/instances/" provider-id)
                 http (fn [method endpoint] (let [value (call :http native-http method endpoint headers)] (when (= "GET" method) (parse-json value))))
                 current (fn [] (let [instance (:instance (http "GET" url))]
                                  (require-valid (and (= provider-id (:id instance)) (string? (:power_status instance)))) instance))
                 initial (current)
                 instance (if (= (:state target) (:power_status initial)) initial
                              (do (http "POST" (str url "/" (:action target)))
                                  (let [deadline (+ (System/nanoTime) (* wait 1000000000))]
                                    (loop [remaining (+ 2 (quot (long wait) 5))]
                                      (let [instance (current)]
                                        (if (= (:state target) (:power_status instance)) instance
                                          (do (require-valid (and (> remaining 1) (< (System/nanoTime) deadline)))
                                              (call :sleep #(Thread/sleep (long (* % 1000))) (min 5 (max 0 (/ (- deadline (System/nanoTime)) 1000000000.0))))
                                              (recur (dec remaining)))))))))]
             (require-valid (= (:state target) (:power_status instance)))
             (cond-> {:status "ready"} (= action "start") (assoc :ip (ip (:main_ip instance))))))]
     (safe-output result environment) result)))
(defn provider-power
  ([opts action provider-id environment] (provider-power opts action provider-id environment {}))
  ([opts action provider-id environment dependencies]
   (try (internal-provider-power opts action provider-id environment dependencies)
        (catch InterruptedException e (throw e))
        (catch Exception _ (throw (ex-info "compute power refused" {}))))))
(defn power-deployment
  ([opts action] (power-deployment opts action (into {} (System/getenv)) {}))
  ([opts action environment] (power-deployment opts action environment {}))
  ([opts action environment dependencies]
   (let [owner (atom nil) acquired (atom false) dispatched (atom false)
         call (fn [key fallback & args] (apply (get dependencies key fallback) args))]
     (letfn [(existing [] (let [observed (call :journal-get journal/journal-get opts environment)] (require-valid (= "present" (:status observed))) observed))
             (valid [doc]
               (require-valid (and (lifecycle/valid-document? doc) (= "active" (:status doc)) (:topology_declared doc)
                                   (= "ready" (get-in doc [:shared :phase])) (= "prepared" (get-in doc [:key :phase]))))
               (let [active (remove #(= "destroyed" (:phase (val %))) (:nodes doc))]
                 (require-valid (and (= 1 (count active)) (:desired (val (first active))) (= "ready" (:phase (val (first active))))))
                 (require-valid (every? #(or (not= "destroyed" (:phase %)) (not (:desired %))) (vals (:nodes doc))))
                 (first active)))
             (execute []
               (let [descriptor (get descriptors (keyword (:provider-compute opts)))]
                 (require-valid (get-in descriptor [:actions (keyword action)]))
                 (if (or (contains? #{:build "build"} (:green/event opts)) (true? (:green/dry-run opts))) {:status "planned" :action action}
                   (let [observed (existing) _ (valid (:document observed)) _ (require-valid (= "idle" (get-in observed [:document :lock :state])))]
                     (reset! owner (coordinator/coordinator opts environment existing #(call :journal-put journal/journal-put opts % environment) nil {:event-prefix "lifecycle/"}))
                     (coordinator/acquire! @owner) (reset! acquired true)
                     (let [doc (:document (coordinator/snapshot @owner)) [id record] (valid doc) node-id (name id) selected (ssh/mode opts)
                           _ (require-valid (= (:mode selected) (get-in doc [:key :mode])))
                           shared (call :read-state runtime/read-state opts (:shared (compute/state-keys (:profile opts) [])) environment)
                           _ (require-valid (and (= "present" (:status shared)) (= (:provider-compute opts) (get-in shared [:params :provider]))))
                           state (call :read-state runtime/read-state opts (:state_key record) environment)
                           _ (require-valid (= "present" (:status state)))
                           declarations [{:node_id node-id :role (:role record) :index (:index record) :provider (:provider-compute opts)}]
                           cluster (compute/collect declarations [(:params state)] node-id)
                           provider-id (get-in cluster [:nodes 0 :provider_id])
                           _ (require-valid (and (string? provider-id) (re-matches (re-pattern (:id_pattern descriptor)) provider-id)))
                           _ (safe-output provider-id environment) _ (reset! dispatched true)
                           powered (call :provider-power provider-power opts action provider-id environment)
                           _ (require-valid (= "ready" (:status powered)))
                           cluster (cond-> cluster (= action "start") (assoc-in [:nodes 0 :ip] (ip (:ip powered))))
                           identity (if (= "managed" (:mode selected)) (str (io/file (or (get environment "HOME") (System/getProperty "user.home")) ".ssh" (:profile opts))) (:private_key_path selected))
                           cluster (cond-> cluster identity (assoc-in [:nodes 0 :ssh_identity_file] identity))
                           key (cond-> {:mode (:mode selected)} identity (assoc :private_key_path identity))
                           result {:status "ready" :action action :cluster cluster :key key}]
                       (safe-output result environment) (coordinator/release! @owner) (reset! acquired false) result)))))]
       (try (execute)
            (catch InterruptedException error (throw error))
            (catch Exception _
              (when (and @acquired (not @dispatched)) (try (coordinator/release! @owner) (catch Exception _ nil)))
              {:status "error"}))))))
