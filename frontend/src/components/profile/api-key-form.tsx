"use client";

import { useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { getApiKey, setApiKey } from "@/lib/api";

export function ApiKeyForm() {
  const [key, setKey] = useState("");
  const [saved, setSaved] = useState(false);

  useEffect(() => {
    setKey(getApiKey() ?? "");
  }, []);

  const handleSave = () => {
    setApiKey(key || null);
    setSaved(true);
    setTimeout(() => setSaved(false), 2000);
  };

  return (
    <div className="flex gap-2">
      <Input
        type="password"
        value={key}
        onChange={(e) => setKey(e.target.value)}
        placeholder="Your Railway API key"
        className="flex-1"
      />
      <Button onClick={handleSave}>
        {saved ? "Saved!" : "Save"}
      </Button>
    </div>
  );
}