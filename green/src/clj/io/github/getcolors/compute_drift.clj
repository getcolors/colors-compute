(ns io.github.getcolors.compute-drift
  "Serialized zero-change plans for a fully converged deployment."
  (:require [clojure.java.io :as io]
            [clojure.string :as str]
            [io.github.getcolors.compute :as compute]
            [io.github.getcolors.compute-coordinator :as coordinator]
            [io.github.getcolors.compute-journal :as journal]
            [io.github.getcolors.compute-lifecycle :as lifecycle]
            [io.github.getcolors.compute-runtime :as runtime]
            [io.github.getcolors.compute-execution :as execution]
            [io.github.getcolors.compute-ssh :as ssh]
            [io.github.getcolors.compute-key-request :as key-request]
            [io.github.getcolors.compute-deployment-request :as deployment]
            [io.github.getcolors.compute-request :as request])
  (:import [java.nio.file Files LinkOption OpenOption]
           [java.nio ByteBuffer]
           [java.nio.charset StandardCharsets CodingErrorAction]))
(defn- require-valid [v] (when-not v (throw (ex-info "compute drift refused" {}))))
(defn- public-key [opts fingerprint environment]
  (let [directory (.toPath (io/file (or (get environment "HOME") (System/getProperty "user.home")) ".ssh"))
        path (.resolve directory (str (:profile opts) ".pub")) nofollow (into-array LinkOption [LinkOption/NOFOLLOW_LINKS])]
    (require-valid (and (not (Files/isSymbolicLink directory)) (Files/isDirectory directory nofollow) (Files/isRegularFile path nofollow)))
    (with-open [stream (Files/newInputStream path (into-array OpenOption [LinkOption/NOFOLLOW_LINKS]))]
      (let [data (.readNBytes stream 65537) _ (require-valid (<= (alength data) 65536))
            decoder (doto (.newDecoder StandardCharsets/UTF_8) (.onMalformedInput CodingErrorAction/REPORT) (.onUnmappableCharacter CodingErrorAction/REPORT))
            value (str/trim (str (.decode decoder (ByteBuffer/wrap data))))]
        (require-valid (= fingerprint (#'ssh/fingerprint value))) value))))
(defn check-deployment-drift
  ([opts topology requirements] (check-deployment-drift opts topology requirements (into {} (System/getenv)) {}))
  ([opts topology requirements environment] (check-deployment-drift opts topology requirements environment {}))
  ([opts topology requirements environment dependencies]
   (let [owner (atom nil) acquired (atom false) cancelled (atom nil) declarations (atom [])]
     (letfn [(call [name default & args] (apply (get dependencies name default) args))
             (valid-document! [doc]
               (require-valid (and (lifecycle/valid-document? doc) (= "active" (:status doc)) (:topology_declared doc)
                                   (= "ready" (get-in doc [:shared :phase])) (= "prepared" (get-in doc [:key :phase]))))
               (require-valid (every? #(or (not= "destroyed" (:phase %)) (not (:desired %))) (vals (:nodes doc))))
               (let [active (into {} (remove #(= "destroyed" (:phase (val %))) (:nodes doc)))]
                 (require-valid (= (count active) (count @declarations)))
                 (doseq [node @declarations]
                   (let [record (get active (keyword (:node_id node)))]
                     (require-valid (and (= "ready" (:phase record)) (:desired record) (= (:role node) (:role record)) (= (:index node) (:index record))))))))
             (read-existing [] (let [observed (call :journal-get journal/journal-get opts environment)]
                                 (require-valid (= "present" (:status observed))) observed))
             (execute []
               (require-valid (and (not (:green/dry-run opts)) (not (contains? #{:build "build"} (:green/event opts)))))
               (reset! declarations (mapv #(cond-> % (true? (:private requirements)) (assoc :private true)) (compute/expand topology)))
               (require-valid (<= 1 (count @declarations) 1000))
               (let [observed (read-existing)] (valid-document! (:document observed)) (require-valid (= "idle" (get-in observed [:document :lock :state]))))
               (reset! owner (coordinator/coordinator opts environment read-existing #(call :journal-put journal/journal-put opts % environment) nil {:event-prefix "lifecycle/"}))
               (coordinator/acquire! @owner) (reset! acquired true)
               (let [doc (:document (coordinator/snapshot @owner)) _ (valid-document! doc)
                     keys (compute/state-keys (:profile opts) (mapv :node_id @declarations))
                     shared (call :read-state (fn [opts key env] (runtime/read-state opts key env runtime/run-command true)) opts (:shared keys) environment)
                     _ (require-valid (and (= "present" (:status shared)) (= (:provider-compute opts) (get-in shared [:params :provider]))))
                     results (mapv (fn [node] (let [state (call :read-state runtime/read-state opts (get-in keys [:nodes (:node_id node)]) environment)]
                                               (require-valid (= "present" (:status state))) (:params state))) @declarations)
                     cluster (compute/collect @declarations results (get requirements :entry_node_id (:node_id (first @declarations))))
                     errors (call :compute-credential-errors compute/compute-credential-errors opts environment)]
                 (if (seq errors) {:status "error" :errors errors}
                     (let [selected (ssh/mode opts) _ (require-valid (= (:mode selected) (get-in doc [:key :mode])))
                           selected (cond-> selected (= "managed" (:mode selected)) (assoc :public_key (call :public-key public-key opts (get-in doc [:key :fingerprint]) environment)))
                           public (call :key-request key-request/key-request opts selected environment)
                           assembly (deployment/deployment-requests opts topology requirements public)
                           shared-request (cond-> (:shared assembly) (contains? (:shared assembly) :roles)
                                            (assoc :peers (into {} (map (fn [node] [(:node_id node) (select-keys node [:role :vpc_ip])]) (:nodes cluster)))))
                           plans (into [[(:shared keys) (:documents (request/provider-request opts "shared" shared-request))]]
                                       (map (fn [node] [(get-in keys [:nodes (:node_id node)]) (:documents (request/provider-request opts "node" node (:outputs shared)))]) (:nodes assembly)))]
                       (doseq [[key documents] plans] (require-valid (= {:status "clean"} (call :check-state execution/check-state opts key documents environment))))
                       {:status "clean"}))))]
       (let [result (try (execute) (catch InterruptedException e (reset! cancelled e) {:status "error"}) (catch Exception _ {:status "error"}))
             result (if @acquired (try (coordinator/release! @owner) result (catch InterruptedException e (reset! cancelled e) {:status "error"}) (catch Exception _ {:status "error"})) result)]
         (if @cancelled (throw @cancelled) result))))))
