(ns io.github.getcolors.compute-registration-test
  (:require [clojure.test :refer [deftest is]] [cheshire.core :as json]
            [io.github.getcolors.compute-registration :as r]))
(def opts {:profile "demo" :provider-compute "digitalocean"})
(def env {"COLORS_PAR_DO_TOKEN" "synthetic-api-secret"})
(defn body [value] (.getBytes (json/generate-string value) "UTF-8"))
(deftest paginated-collision-and-ownership-checks
  (let [calls (atom []) http (fn [url headers]
                             (swap! calls conj url) (is (= "Bearer synthetic-api-secret" (get headers "Authorization")))
                             (body (if (= 1 (count @calls)) {:ssh_keys [] :links {:pages {:next "https://api.digitalocean.com/v2/account/keys?per_page=200&page=2"}}}
                                       {:ssh_keys [{:id 42 :name "demo" :public_key "ssh-ed25519 AAAA comment"}]})))]
    (is (= {:status "checked"} (r/registration-preflight opts "managed" {:provider "digitalocean" :scope "account" :id 42} "ssh-ed25519 AAAA" env http)))
    (is (= 2 (count @calls)))))
(deftest foreign-collision-and-cross-origin-pagination-refuse
  (is (thrown-with-msg? Exception #"foreign SSH registration"
                        (r/registration-preflight opts "managed" nil nil env
                                                  (fn [& _] (body {:ssh_keys [{:id 1 :name "demo" :public_key "ssh-ed25519 AAAA"}]})))))
  (let [calls (atom 0)]
    (is (thrown-with-msg? Exception #"SSH registration preflight failed"
                          (r/registration-preflight opts "managed" nil nil env
                                                    (fn [& _] (swap! calls inc) (body {:ssh_keys [] :links {:pages {:next "https://evil.example/keys?page=2"}}})))))
    (is (= 1 @calls))))
