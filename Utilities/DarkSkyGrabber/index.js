// includes
const request = require("sync-request");
const jsonfile = require('jsonfile');
const fs = require('fs');

// loading configuration files
const config = jsonfile.readFileSync('DarkSkyGrabber\\config.json');

const coordinate = jsonfile.readFileSync('DarkSkyGrabber\\coordinate.json');
var place = 'Noordwijkerhout' // 'Grad_Jablje' or 'Aarhus' or 'Den_Helder' or 'Medemblik' or see in 'coordinte.json'

// setting up environmental variables regarding time
process.env.TZ = 'Europe/Ljubljana'
//let a = new Date("Fri Jul 20 2018 00:00:00 GMT+0100(CET)");
let a = new Date("Fri Jan 1 2016 00:00:00 GMT+0100(CET)");
let timeStamp = Math.floor(a.getTime()/1000);      
console.log(timeStamp);
console.log(process.env.TZ)
//let timeStampNOW = new Date("Sun Jan 1 2017 00:00:00 GMT+0100(CET)");
let timeStampNOW = Math.floor((Date.now()+7200000)/1000);

// if it exists, read the last written timestamp
if (fs.existsSync("DarkSkyGrabber\\data\\data_" + place + ".json")) {
    console.log("Reading last timestamp!")
    let lines = fs.readFileSync("DarkSkyGrabber\\data\\data_" + place + ".json").toString().split("\n");
    //console.log(lines[lines.length - 2]);
    let line = JSON.parse(lines[lines.length - 2]);
    timeStamp = line.daily.data[0].time;
    // add an hour
    timeStamp = timeStamp + 24*3600;
}

// gathering data until today
console.log(timeStampNOW)
//console.log(timeStamp)

// starting the loop
let lat = coordinate[place].lat;
let longitude = coordinate[place].longitude;
let token = config["darkSky-token"];

 
while (timeStamp <= timeStampNOW) {
    var date = new Date(timeStamp * 1000);
    console.log(date);    

    var url = "https://api.darksky.net/forecast/" + token + "/" + 
    lat +"," + 
    longitude + "," + 
    timeStamp + 
    '?exclude=currently,minutely,hourly,alerts,flags'+ //only daily
    '&units=si';  //units

    var res = request("GET", url);

    fs.appendFileSync("DarkSkyGrabber\\data\\data_" + place + ".json", res.getBody('utf8') + "\n");
   
    //console.log(JSON.parse(res.getBody('utf8')).daily.data[0]);  

    // adding a day to the data
    timeStamp = timeStamp + 24 * 3600;
}
