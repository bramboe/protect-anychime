/* Pure onset estimator; raw microphone samples never leave the browser. */
(function(root){class OnsetDetector{
 constructor(){this.noise=[];this.threshold=null;this.candidate=null;this.onset=null;}
 observe(time,rms){if(!Number.isFinite(time)||!Number.isFinite(rms))return null;if(this.threshold===null){this.noise.push(rms);if(this.noise.length>400)this.noise.shift();return null;}if(this.onset!==null)return this.onset;if(rms>this.threshold){if(this.candidate!==null){this.onset=this.candidate;return this.onset;}this.candidate=time;}else this.candidate=null;return null;}
 arm(){if(this.noise.length<30)throw Error('Not enough microphone input. Check microphone access.');const values=[...this.noise].sort((a,b)=>a-b);const noise=values[Math.floor(values.length/2)];if(noise>.025)throw Error('The room is too noisy. Stop other audio and try again.');this.threshold=Math.max(.002,noise*6);this.candidate=null;this.onset=null;return this.threshold;}
 reset(){this.threshold=null;this.candidate=null;this.onset=null;this.noise=[];}
}root.CalibrationOnset=OnsetDetector;if(typeof module!=='undefined')module.exports=OnsetDetector;})(globalThis);
